"""Start-gun signal straight from the EtherLynx camera, independent of FinishLynx.

Reverse-engineered 2026-09-12 from a live capture (etherlynx/captures/06):
FinishLynx polls the camera (Kirk type 0x03, host -> camera:41925) about 6x/s
and the camera answers with a 100-byte status reply. Words are LE u32 counted
after the 8-byte envelope (word n at offset 8 + 4n):

    off  0..3   magic 94 8d 96 b4
    off  4..7   sequence echo (LE u32)
    off 16..19  poll counter (LE u32, +1 per reply)
    off 24..27  word 4: camera timebase, 10 000 ticks/s (measured 10 000.4/s)
    off 28..31  word 5: input event type - 0 normally, else the event of this
                snapshot (firmware, 2026-10-11):
                  1                    wired start sensor          -> gun
                  2, 3, 4              other inputs (photo-eyes)
                  0x1000_0000 | code   RadioLynx; code in the upper 16 bits:
                                       0x11xx start impulse (0x1140 seen) -> gun
                                       0x12xx transmitter status (0x120b seen)
    off 32..35  word 6: event timestamp, same 10 kHz timebase
    off 43      top byte of word 8: extra field (0x01 alongside 0x1140)

Each new snapshot carries at most one event (the camera queues up to 32 per
source), so an event must be caught live. A repeated request token returns
the cached snapshot, so the same event can show up in two replies. Only the
100-byte status replies count: the 48-byte hardware-info reply to 0x1a on
every connect has the same magic but no status words (2026-10-11).

We sniff the camera's replies with tshark/Npcap (no admin needed on this PC),
keep a running estimate of wall-clock = timebase offset from the ordinary
replies, and turn each event into a wall-clock gun time good to a few
milliseconds - the reply arrives ~160 ms after the shot but the embedded
timestamp is the camera's own.
"""

import ipaddress
import logging
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPLY_MAGIC = bytes.fromhex("948d96b4")
TICKS_PER_SEC = 10_000.0
START_CODE = 0x1140             # RadioLynx start impulse as first captured
STATUS_REPLY_LEN = 100          # status reply to 0x03; nothing else is decoded (2026-10-11)
OFF_TIMEBASE = 24               # word 4
OFF_EVENT = 28                  # word 5: event type
OFF_EVENT_TS = 32               # word 6: event timestamp
OFF_FLAG = 43                   # top byte of word 8
RESTART_DELAY_SEC = 5.0
OFFSET_WINDOW = 40              # replies used for the timebase->wall fit
OLD_EVENT_SEC = 60.0            # warn about events older than this (camera clock)
DEDUP_WINDOW_SEC = 600.0        # the same (type, timestamp) twice in this window is one event
RESYNC_BACK_SEC = 1.0           # camera clock stepping back more than this = re-booted camera
RELEARN_AFTER_SEC = 10.0        # auto host: follow another camera once ours is this silent

WIRED_START = 1
PHOTO_EYES = (2, 3, 4)
RADIOLYNX = 0x1000_0000
GUN_KINDS = ("wired start", "RadioLynx start")

AUTO_HOST = "auto"
# The camera_host default until 2026-10-11. FinishLynx gives the camera its
# PC's camera-NIC address + 1 (.67 next to a PC at .66), so a fixed address
# missed the camera on most PCs. Every config.yaml written before then holds
# this value because first run copies config.example.yaml and the console
# saves the whole config - not because anyone chose it - so it counts as auto
# (2026-10-11). A camera really at .201 is still found; only pinning .201 out
# of two cameras is lost.
LEGACY_DEFAULT_HOST = "192.168.0.201"
AUTO_SUBNET_HINT = LEGACY_DEFAULT_HOST   # EtherLynx factory subnet, tried first in auto


def normalize_camera_host(value) -> str:
    """camera_host from config/console: '', 'auto' and the old default mean auto."""
    v = str(value or "").strip()
    if not v or v.lower() == AUTO_HOST or v == LEGACY_DEFAULT_HOST:
        return AUTO_HOST
    return v


def event_code(word5: int) -> int:
    """Word 5 as shown: a RadioLynx event's code (upper 16 bits, e.g. 0x1140),
    else the word itself (1 = wired start, 2..4 = photo-eyes)."""
    return word5 >> 16 if word5 & 0xF000_0000 == RADIOLYNX else word5


def event_kind(code: int) -> str:
    if code == WIRED_START:
        return "wired start"
    if code in PHOTO_EYES:
        return "photo-eye"
    if code >> 8 == 0x11:
        return "RadioLynx start"
    if code >> 8 == 0x12:
        return "RadioLynx status"
    return "unknown"


class CameraEvent:
    __slots__ = ("code", "camera_ts", "wall", "reply_wall", "flag")

    def __init__(self, code: int, camera_ts: int, wall: datetime, reply_wall: float, flag: int):
        self.code = code
        self.camera_ts = camera_ts
        self.wall = wall
        self.reply_wall = reply_wall
        self.flag = flag

    @property
    def kind(self) -> str:
        return event_kind(self.code)

    @property
    def is_start(self) -> bool:
        """A start gun: the wired start sensor or a RadioLynx start impulse."""
        return self.kind in GUN_KINDS

    def as_dict(self) -> Dict:
        return {"code": f"0x{self.code:04x}", "kind": self.kind, "camera_ts": self.camera_ts,
                "wall": self.wall.isoformat(), "is_start": self.is_start}


class ReplyDecoder:
    """Stateful decoder: feed (arrival_epoch, payload) for each camera reply;
    returns a CameraEvent when the reply carries a new input event."""

    def __init__(self):
        self._offsets: Deque[float] = deque(maxlen=OFFSET_WINDOW)   # wall - tb/1e4
        self._seen: Deque[Tuple[int, int, float]] = deque(maxlen=64)  # (word 5, word 6, arrival)
        self.replies = 0
        self.resyncs = 0
        self.last_reply_at = 0.0
        self.last_timebase: Optional[int] = None

    @staticmethod
    def is_reply(payload: bytes) -> bool:
        # Exactly 100 bytes: until 2026-10-11 anything >= 44 bytes passed, so
        # the 48-byte hardware-info reply on every connect read as an event
        # (code 0x0f17, really MAC bytes) and fed a garbage clock sample.
        return len(payload) == STATUS_REPLY_LEN and payload[:4] == REPLY_MAGIC

    @property
    def offset(self) -> Optional[float]:
        return statistics.median(self._offsets) if self._offsets else None

    def _duplicate(self, word5: int, ev_ts: int, arrival_epoch: float) -> bool:
        for w, ts, at in self._seen:
            if w == word5 and ts == ev_ts and arrival_epoch - at < DEDUP_WINDOW_SEC:
                return True
        self._seen.append((word5, ev_ts, arrival_epoch))
        return False

    def feed(self, arrival_epoch: float, payload: bytes) -> Optional[CameraEvent]:
        if not self.is_reply(payload):
            return None
        tb = struct.unpack_from("<I", payload, OFF_TIMEBASE)[0]
        self.replies += 1
        self.last_reply_at = arrival_epoch
        prev = self.last_timebase
        if prev is not None and prev - tb > RESYNC_BACK_SEC * TICKS_PER_SEC:
            # The camera clock went back: the camera was power-cycled and
            # FinishLynx re-booted it (or the 32-bit counter wrapped, ~5 days).
            # The old samples describe another clock - start the fit over
            # instead of letting the median drift across ~20 replies (2026-10-11).
            logger.info(f"camera clock stepped back {(prev - tb) / TICKS_PER_SEC:.1f} s "
                        "(camera re-booted?) - re-syncing")
            self._offsets.clear()
            self.resyncs += 1
            prev = None
        if prev is None or tb > prev:
            # A repeated token returns the cached snapshot (same timebase,
            # later arrival): not a clock sample.
            self.last_timebase = tb
            self._offsets.append(arrival_epoch - tb / TICKS_PER_SEC)
        word5 = struct.unpack_from("<I", payload, OFF_EVENT)[0]
        if word5 == 0:
            return None
        ev_ts = struct.unpack_from("<I", payload, OFF_EVENT_TS)[0]
        if self._duplicate(word5, ev_ts, arrival_epoch):
            logger.debug(f"camera event 0x{word5:08x} @ {ev_ts} repeated (cached snapshot) - ignored")
            return None
        # Wall time of the event: the camera stamped it on its own clock, so
        # map through the fitted offset (jitter of the poll replies averages
        # out). Age is signed and modulo 2^32 so a wrap in between is harmless.
        age_ticks = ((tb - ev_ts + 0x8000_0000) & 0xFFFF_FFFF) - 0x8000_0000
        wall_epoch = (tb - age_ticks) / TICKS_PER_SEC + self.offset
        code = event_code(word5)
        age = age_ticks / TICKS_PER_SEC
        if age > OLD_EVENT_SEC or age < -RESYNC_BACK_SEC:
            # Until 2026-10-11 such an event got the reply's arrival time; the
            # camera's own stamp is right however late the reply is.
            logger.warning(f"camera event 0x{code:04x} is {age:.0f} s old by the camera clock "
                           f"- using its own time {datetime.fromtimestamp(wall_epoch).strftime('%H:%M:%S.%f')[:-3]}")
        return CameraEvent(code, ev_ts, datetime.fromtimestamp(wall_epoch), arrival_epoch,
                           payload[OFF_FLAG])


# ------------------------------------------------------------------ tooling

def find_tshark(configured: str = "auto") -> Optional[str]:
    if configured and configured != "auto":
        return configured if os.path.exists(configured) else None
    found = shutil.which("tshark")
    if found:
        return found
    for p in (r"C:\Program Files\Wireshark\tshark.exe",
              r"C:\Program Files (x86)\Wireshark\tshark.exe",
              "/usr/bin/tshark", "/usr/local/bin/tshark"):
        if os.path.exists(p):
            return p
    return None


def list_capture_interfaces(tshark: str) -> List[Tuple[str, str]]:
    """[(device, friendly_name)] from `tshark -D`."""
    try:
        out = subprocess.run([tshark, "-D"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"camera start: tshark -D failed: {e}")
        return []
    result = []
    for line in out.splitlines():
        m = re.match(r"\s*\d+\.\s+(\S+)(?:\s+\((.*)\))?", line)
        if m:
            result.append((m.group(1), (m.group(2) or "").strip()))
    return result


def _windows_ipv4_addresses() -> List[Tuple[str, str]]:
    """[(ip, adapter alias)] for this PC's IPv4 addresses (Windows)."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.IPAddress + '|' + $_.InterfaceAlias }"],
            capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for line in out.splitlines():
        ip, _, alias = line.strip().partition("|")
        if ip and alias:
            result.append((ip, alias))
    return result


def _is_lan(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback      # 10/8, 172.16/12, 192.168/16, 169.254/16


def pick_interfaces(tshark: str, camera_host: str, configured: str = "auto") -> List[str]:
    """Capture devices: the configured one (GUID/device/alias), else the NIC on
    the camera's /24. With the camera address unknown (auto), the NIC on the
    EtherLynx factory subnet 192.168.0.x, else every LAN adapter - FinishLynx
    gives the camera the address after its own NIC's, whatever that is, and
    the capture filter keeps the rest out (2026-10-11)."""
    interfaces = list_capture_interfaces(tshark)
    if configured and configured != "auto":
        for dev, name in interfaces:
            if configured in (dev, name) or configured.lower() in dev.lower():
                return [dev]
        return [configured]  # let tshark complain if it is wrong
    auto = not camera_host or camera_host == AUTO_HOST     # a learned .201 is a real address
    hint = AUTO_SUBNET_HINT if auto else camera_host
    prefix = hint.rsplit(".", 1)[0] + "."
    addrs = _windows_ipv4_addresses()
    aliases = [alias for ip, alias in addrs if ip.startswith(prefix)][:1]
    if not aliases and auto:
        aliases = list(dict.fromkeys(alias for ip, alias in addrs if _is_lan(ip)))
    return [dev for alias in aliases for dev, name in interfaces if name == alias]


# ------------------------------------------------------------------ listener

class CameraStartListener:
    """Runs tshark on the camera NIC and reports start-sensor events.

    on_event(CameraEvent) is called from the reader thread for every input
    event (check .is_start for the gun). Restarts tshark if it exits.
    camera_host 'auto' (the default) hears any camera and follows the first
    one that sends a status reply; its address is in status()["camera_ip"].
    """

    def __init__(self, camera_host: str = AUTO_HOST, camera_port: int = 41925,
                 interface: str = "auto", tshark_path: str = "auto",
                 on_event: Optional[Callable[[CameraEvent], None]] = None,
                 popen: Callable = subprocess.Popen):
        self.camera_host = normalize_camera_host(camera_host)
        self.auto_host = self.camera_host == AUTO_HOST
        self.camera_ip: Optional[str] = None if self.auto_host else self.camera_host
        self.camera_port = camera_port
        self.interface_pref = interface
        self.tshark_pref = tshark_path
        self.on_event = on_event
        self._popen = popen
        self.decoder = ReplyDecoder()
        self.events: Deque[CameraEvent] = deque(maxlen=50)
        self.starts: Deque[CameraEvent] = deque(maxlen=50)
        self.interface: Optional[str] = None
        self.tshark: Optional[str] = None
        self.error: Optional[str] = None
        self._other_cameras: set = set()
        self._proc = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -------------------------------------------------------------- status

    @property
    def is_running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    @property
    def is_receiving(self) -> bool:
        return self.is_running and time.time() - self.decoder.last_reply_at < 5.0

    def status(self) -> Dict:
        with self._lock:
            last = self.events[-1] if self.events else None
            last_start = self.starts[-1] if self.starts else None
            n_starts = len(self.starts)
        if self.camera_ip:
            camera = f"{self.camera_ip}:{self.camera_port}" + (" (found automatically)" if self.auto_host else "")
        else:
            camera = f"auto - not heard yet (port {self.camera_port})"
        return {
            "running": self.is_running,
            "receiving": self.is_receiving,
            "interface": self.interface,
            "camera": camera,
            "camera_ip": self.camera_ip,
            "camera_auto": self.auto_host,
            "replies": self.decoder.replies,
            "last_reply_age_sec": (round(time.time() - self.decoder.last_reply_at, 1)
                                   if self.decoder.last_reply_at else None),
            "events": len(self.events),
            "starts": n_starts,
            "last_event": last.as_dict() if last else None,
            "last_start": last_start.wall.isoformat() if last_start else None,
            "error": self.error,
        }

    # -------------------------------------------------------------- lifecycle

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-start", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._kill()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _kill(self):
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def capture_filter(self) -> str:
        # udp[4:2] is the UDP length (payload + 8): only the 100-byte status
        # replies get through, not the 1.4-kB image blocks (2026-10-11).
        host = "" if self.auto_host else f" and src host {self.camera_host}"
        return f"udp src port {self.camera_port}{host} and udp[4:2] == {STATUS_REPLY_LEN + 8}"

    def _command(self) -> Optional[List[str]]:
        self.tshark = find_tshark(self.tshark_pref)
        if not self.tshark:
            self.error = "tshark not found (install Wireshark with Npcap)"
            return None
        devices = pick_interfaces(self.tshark, self.camera_ip or AUTO_HOST, self.interface_pref)
        if not devices:
            self.error = (f"no capture interface on the camera's subnet ({self.camera_ip})" if self.camera_ip
                          else "no capture interface found - set the camera IP or the interface")
            return None
        self.interface = ", ".join(devices)
        self.error = None
        cmd = [self.tshark]
        for dev in devices:
            cmd += ["-i", dev]
        return cmd + ["-l", "-n", "-q", "-f", self.capture_filter(),
                      "-T", "fields", "-e", "frame.time_epoch", "-e", "data.data", "-e", "ip.src"]

    def _run(self):
        while not self._stop.is_set():
            cmd = self._command()
            if cmd is None:
                logger.warning(f"camera start listener idle: {self.error}")
                self._stop.wait(30.0)
                continue
            try:
                self._proc = self._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                self.error = f"could not start tshark: {e}"
                logger.warning(self.error)
                self._stop.wait(RESTART_DELAY_SEC)
                continue
            logger.info(f"camera start listener: tshark on {self.interface}, "
                        f"watching {self.camera_ip or 'any camera'}:{self.camera_port}")
            try:
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    self._handle_line(line)
            except (OSError, ValueError) as e:
                logger.debug(f"camera start reader ended: {e}")
            err = ""
            try:
                err = (self._proc.stderr.read() or "").strip()
            except Exception:
                pass
            if self._stop.is_set():
                break
            self.error = err.splitlines()[-1] if err else "tshark exited"
            logger.warning(f"camera start listener: {self.error} - restarting in {RESTART_DELAY_SEC:.0f}s")
            self._stop.wait(RESTART_DELAY_SEC)

    def _from_our_camera(self, src: str, arrival: float) -> bool:
        """Auto host: learn the camera from its first status reply and keep to
        it, so a second camera's clock never mixes into the fit (2026-10-11)."""
        if not self.auto_host or not src or src == self.camera_ip:
            return True
        if self.camera_ip is None:
            self.camera_ip = src
            logger.info(f"camera start: camera found at {src}")
            return True
        if arrival - self.decoder.last_reply_at > RELEARN_AFTER_SEC:
            logger.warning(f"camera start: {self.camera_ip} silent - now following the camera at {src}")
            replies = self.decoder.replies
            self.decoder = ReplyDecoder()          # another camera, another clock
            self.decoder.replies = replies
            self.camera_ip = src
            return True
        if src not in self._other_cameras:
            self._other_cameras.add(src)
            logger.info(f"camera start: also hearing a camera at {src} - ignored, following "
                        f"{self.camera_ip} (set the camera IP to choose)")
        return False

    def _handle_line(self, line: str):
        # fields: frame.time_epoch, data.data, ip.src
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 2 or not parts[1]:
            return
        try:
            arrival = float(parts[0])
            payload = bytes.fromhex(parts[1].replace(":", ""))
        except ValueError:
            return
        if not ReplyDecoder.is_reply(payload):
            return
        src = parts[2].strip() if len(parts) > 2 else ""
        if not self._from_our_camera(src, arrival):
            return
        ev = self.decoder.feed(arrival, payload)
        if ev is None:
            return
        with self._lock:
            self.events.append(ev)
            if ev.is_start:
                self.starts.append(ev)
        # RadioLynx status messages and photo-eyes are routine: debug only.
        level = logging.DEBUG if ev.kind in ("RadioLynx status", "photo-eye") else logging.INFO
        logger.log(level, f"camera input event code=0x{ev.code:04x} ({ev.kind}) at "
                          f"{ev.wall.strftime('%H:%M:%S.%f')[:-3]}" + (" (START)" if ev.is_start else ""))
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                logger.exception("camera start callback failed")


def replay_pcap(path: str, camera_host: str = AUTO_HOST, camera_port: int = 41925,
                tshark_path: str = "auto") -> List[CameraEvent]:
    """Offline: run the decoder over a saved capture (used by tests/tools)."""
    tshark = find_tshark(tshark_path)
    if not tshark:
        raise RuntimeError("tshark not found")
    host = normalize_camera_host(camera_host)
    flt = f"udp.srcport=={camera_port} && udp.length=={STATUS_REPLY_LEN + 8}"
    if host != AUTO_HOST:
        flt += f" && ip.src=={host}"
    out = subprocess.run([tshark, "-r", path, "-Y", flt,
                          "-T", "fields", "-e", "frame.time_epoch", "-e", "data.data"],
                         capture_output=True, text=True, timeout=300).stdout
    dec = ReplyDecoder()
    events = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[1]:
            continue
        ev = dec.feed(float(parts[0]), bytes.fromhex(parts[1].replace(":", "")))
        if ev:
            events.append(ev)
    return events


if __name__ == "__main__":
    # python -m rfid_finishlynx.camera_start capture.pcapng
    for e in replay_pcap(sys.argv[1]):
        print(f"{e.wall.strftime('%H:%M:%S.%f')[:-3]}  code=0x{e.code:04x} ({e.kind})  flag={e.flag}  "
              f"camera_ts={e.camera_ts}  reply {e.reply_wall - e.wall.timestamp():+.3f}s later")
