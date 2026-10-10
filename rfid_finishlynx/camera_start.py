"""Start-gun signal straight from the EtherLynx camera, independent of FinishLynx.

Reverse-engineered 2026-09-12 from a live capture (etherlynx/captures/06):
FinishLynx polls the camera (Kirk type 0x03, host -> camera:41925) about 6x/s
and the camera answers with a 100-byte status reply:

    off  0..3   magic 94 8d 96 b4
    off  4..7   sequence echo (LE u32)
    off 16..19  poll counter (LE u32, +1 per reply)
    off 24..27  camera timebase, LE u32, 10 000 ticks/s (measured 10 000.4/s)
    off 30..31  input event code (LE u16) - 0 normally; 0x1140 when the
                start sensor fired since the previous poll (0x120b seen once
                for a different input)
    off 32..35  event timestamp, LE u32, same 10 kHz timebase
    off 43      0x01 alongside code 0x1140

The event fields are non-zero in exactly one reply per start, so the record
must be caught live. We sniff the camera's replies with tshark/Npcap (no
admin needed on this PC), keep a running estimate of wall-clock = timebase
offset from the ordinary replies, and turn each event into a wall-clock gun
time good to a few milliseconds - the reply arrives ~160 ms after the shot
but the embedded timestamp is the camera's own.
"""

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
START_CODE = 0x1140
OFF_TIMEBASE = 24
OFF_EVENT_CODE = 30
OFF_EVENT_TS = 32
MIN_REPLY_LEN = 44
RESTART_DELAY_SEC = 5.0
OFFSET_WINDOW = 40          # replies used for the timebase->wall fit


class CameraEvent:
    __slots__ = ("code", "camera_ts", "wall", "reply_wall", "flag")

    def __init__(self, code: int, camera_ts: int, wall: datetime, reply_wall: float, flag: int):
        self.code = code
        self.camera_ts = camera_ts
        self.wall = wall
        self.reply_wall = reply_wall
        self.flag = flag

    def as_dict(self) -> Dict:
        return {"code": f"0x{self.code:04x}", "camera_ts": self.camera_ts,
                "wall": self.wall.isoformat(), "is_start": self.code == START_CODE}


class ReplyDecoder:
    """Stateful decoder: feed (arrival_epoch, payload) for each camera reply;
    returns a CameraEvent when the reply carries an input event."""

    def __init__(self):
        self._offsets: Deque[float] = deque(maxlen=OFFSET_WINDOW)   # wall - tb/1e4
        self.replies = 0
        self.last_reply_at = 0.0
        self.last_timebase: Optional[int] = None

    @staticmethod
    def is_reply(payload: bytes) -> bool:
        return len(payload) >= MIN_REPLY_LEN and payload[:4] == REPLY_MAGIC

    @property
    def offset(self) -> Optional[float]:
        return statistics.median(self._offsets) if self._offsets else None

    def feed(self, arrival_epoch: float, payload: bytes) -> Optional[CameraEvent]:
        if not self.is_reply(payload):
            return None
        tb = struct.unpack_from("<I", payload, OFF_TIMEBASE)[0]
        self.replies += 1
        self.last_reply_at = arrival_epoch
        self.last_timebase = tb
        self._offsets.append(arrival_epoch - tb / TICKS_PER_SEC)
        code = struct.unpack_from("<H", payload, OFF_EVENT_CODE)[0]
        if code == 0:
            return None
        ev_ts = struct.unpack_from("<I", payload, OFF_EVENT_TS)[0]
        # Wall time of the event: the camera stamped it on its own clock, so
        # map through the fitted offset (jitter of the poll replies averages
        # out); fall back to "reply time minus age" for the very first reply.
        off = self.offset
        if off is None:
            off = arrival_epoch - tb / TICKS_PER_SEC
        age_ticks = (tb - ev_ts) & 0xFFFFFFFF
        if age_ticks > 60 * TICKS_PER_SEC:
            # Implausible (more than a minute old, or timebase wrapped) - use
            # the reply time itself rather than a wild value.
            wall_epoch = arrival_epoch
        else:
            wall_epoch = ev_ts / TICKS_PER_SEC + off
        flag = payload[43] if len(payload) > 43 else 0
        return CameraEvent(code, ev_ts, datetime.fromtimestamp(wall_epoch), arrival_epoch, flag)


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


def _windows_alias_for_subnet(camera_host: str) -> Optional[str]:
    """Friendly adapter name that shares the camera's /24 (Windows)."""
    if sys.platform != "win32":
        return None
    prefix = camera_host.rsplit(".", 1)[0] + "."
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.IPAddress + '|' + $_.InterfaceAlias }"],
            capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        ip, _, alias = line.strip().partition("|")
        if ip.startswith(prefix) and alias:
            return alias
    return None


def pick_interface(tshark: str, camera_host: str, configured: str = "auto") -> Optional[str]:
    """Capture device: configured (GUID/device/alias) or the NIC on the camera subnet."""
    interfaces = list_capture_interfaces(tshark)
    if configured and configured != "auto":
        for dev, name in interfaces:
            if configured in (dev, name) or configured.lower() in dev.lower():
                return dev
        return configured  # let tshark complain if it is wrong
    alias = _windows_alias_for_subnet(camera_host)
    if alias:
        for dev, name in interfaces:
            if name == alias:
                return dev
    return None


# ------------------------------------------------------------------ listener

class CameraStartListener:
    """Runs tshark on the camera NIC and reports start-sensor events.

    on_event(CameraEvent) is called from the reader thread for every input
    event (check .code == START_CODE for the gun). Restarts tshark if it exits.
    """

    def __init__(self, camera_host: str = "192.168.0.201", camera_port: int = 41925,
                 interface: str = "auto", tshark_path: str = "auto",
                 on_event: Optional[Callable[[CameraEvent], None]] = None,
                 popen: Callable = subprocess.Popen):
        self.camera_host = camera_host
        self.camera_port = camera_port
        self.interface_pref = interface
        self.tshark_pref = tshark_path
        self.on_event = on_event
        self._popen = popen
        self.decoder = ReplyDecoder()
        self.events: Deque[CameraEvent] = deque(maxlen=50)
        self.interface: Optional[str] = None
        self.tshark: Optional[str] = None
        self.error: Optional[str] = None
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
            starts = [e for e in self.events if e.code == START_CODE]
        return {
            "running": self.is_running,
            "receiving": self.is_receiving,
            "interface": self.interface,
            "camera": f"{self.camera_host}:{self.camera_port}",
            "replies": self.decoder.replies,
            "last_reply_age_sec": (round(time.time() - self.decoder.last_reply_at, 1)
                                   if self.decoder.last_reply_at else None),
            "events": len(self.events),
            "starts": len(starts),
            "last_event": last.as_dict() if last else None,
            "last_start": starts[-1].wall.isoformat() if starts else None,
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

    def _command(self) -> Optional[List[str]]:
        self.tshark = find_tshark(self.tshark_pref)
        if not self.tshark:
            self.error = "tshark not found (install Wireshark with Npcap)"
            return None
        self.interface = pick_interface(self.tshark, self.camera_host, self.interface_pref)
        if not self.interface:
            self.error = f"no capture interface on the camera's subnet ({self.camera_host})"
            return None
        self.error = None
        return [self.tshark, "-i", self.interface, "-l", "-n", "-q",
                "-f", f"udp and src host {self.camera_host} and src port {self.camera_port}",
                "-T", "fields", "-e", "frame.time_epoch", "-e", "data.data"]

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
                        f"watching {self.camera_host}:{self.camera_port}")
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

    def _handle_line(self, line: str):
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 2 or not parts[1]:
            return
        try:
            arrival = float(parts[0])
            payload = bytes.fromhex(parts[1].replace(":", ""))
        except ValueError:
            return
        ev = self.decoder.feed(arrival, payload)
        if ev is None:
            return
        with self._lock:
            self.events.append(ev)
        logger.info(f"camera input event code=0x{ev.code:04x} at {ev.wall.strftime('%H:%M:%S.%f')[:-3]}"
                    + (" (START)" if ev.code == START_CODE else ""))
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                logger.exception("camera start callback failed")


def replay_pcap(path: str, camera_host: str = "192.168.0.201", camera_port: int = 41925,
                tshark_path: str = "auto") -> List[CameraEvent]:
    """Offline: run the decoder over a saved capture (used by tests/tools)."""
    tshark = find_tshark(tshark_path)
    if not tshark:
        raise RuntimeError("tshark not found")
    out = subprocess.run([tshark, "-r", path, "-Y",
                          f"udp && ip.src=={camera_host} && udp.srcport=={camera_port}",
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
        print(f"{e.wall.strftime('%H:%M:%S.%f')[:-3]}  code=0x{e.code:04x}  flag={e.flag}  "
              f"camera_ts={e.camera_ts}  reply {e.reply_wall - e.wall.timestamp():+.3f}s later")
