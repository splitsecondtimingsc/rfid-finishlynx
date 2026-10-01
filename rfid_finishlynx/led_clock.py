"""LED race clocks over USB-serial - two interchangeable models.

"ch340" - newer clock on a CH340 adapter (docs/ledclock/LED-Clock-Notes.md,
hardware-verified 2026-09-07): frame 0x1F '0' <cmd> <ascii> 0x09 at 9600 8N1;
only 'R' replies. T1<HHMMSS> sets AND starts a free-running count-up (the
vendor's start/reset commands do not work); M<HHMMSS> sets time-of-day.
We arm it once per gun and re-arm on drift.

"electro" - Electro Numerics Raceclock on a PL2303 adapter
(docs/ledclock/old-electro/PROTOCOL.md, hardware-verified 2026-09-07):
write-only remote display in "rd" mode, frame b"c" + exactly 6 ASCII chars +
CRLF at 9600 8N1, init b"c\r" once; spaces blank digits. It has no timer of
its own, so we stream MMSSt+' ' frames at 10 Hz while racing and HHMMSS
once a second when idle. NOTE: the clock must be in "rd" mode (a clock-side
setting) or it silently ignores everything.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import serial
from serial.tools import list_ports

logger = logging.getLogger(__name__)

CH340_VID, CH340_PID = 0x1A86, 0x7523
PL2303_VID, PL2303_PID = 0x067B, 0x2303

SOH, ETX, ADDR = 0x1F, 0x09, ord("0")

DISCOVER_RETRY_SEC = 5.0
DRIFT_CHECK_SEC = 30.0
VERIFY_AFTER_SET_SEC = 2.0  # ch340: read back this soon after every set/arm (a frame can be lost)
UNANSWERED_BEFORE_RESEND = 2  # ch340: silent read-backs in a row before the set is sent again
RESEND_LOG_LIMIT = 3        # ch340: re-sends logged at INFO before dropping to DEBUG (clock off?)
TOD_TOLERANCE_SEC = 1.5     # ch340: re-set time-of-day when the read-back is further off than this
RACE_TOLERANCE_SEC = 0.75   # ch340: re-arm a running count when it is further than this from race time
GUN_MOVED_SEC = 0.5         # ch340: a gun that shifts by more than this (station offset refined) re-arms
IDLE_RESYNC_SEC = 3600.0  # ch340: refresh time-of-day hourly (RTC may not be battery-backed)

ELECTRO_WIDTH = 6
ELECTRO_INIT = b"c\r"


# ------------------------------------------------------------- ch340 protocol

def frame(cmd: str, data: str = "") -> bytes:
    return bytes([SOH, ADDR]) + cmd.encode("ascii") + data.encode("ascii") + bytes([ETX])


def parse_read_response(raw: bytes) -> Optional[str]:
    """Extract the HHMMSS value from an 'R' response, or None.

    Response: 0xF1 0x1F 0x1F '2' 'R' <6 ascii digits> 0x09 <checksum>,
    checksum = low byte of sum of addr+cmd+data.
    """
    i = raw.rfind(bytes([SOH]))
    if i < 0 or ETX not in raw[i:]:
        return None
    j = raw.index(ETX, i)
    payload = raw[i + 1:j]  # addr + cmd + data
    if len(payload) < 8 or len(raw) <= j + 1:
        return None
    if raw[j + 1] != sum(payload) & 0xFF:
        return None
    data = payload[2:].decode("ascii", "replace")
    return data if len(data) == 6 and data.isdigit() else None


def seconds_to_hhmmss(secs: int) -> str:
    secs = max(0, min(int(secs), 99 * 3600 + 59 * 60 + 59))
    return f"{secs // 3600:02d}{secs % 3600 // 60:02d}{secs % 60:02d}"


def hhmmss_to_seconds(v: str) -> int:
    return int(v[0:2]) * 3600 + int(v[2:4]) * 60 + int(v[4:6])


def time_of_day_payload(now: datetime, hour12: bool = False, blank_leading_zero: bool = False) -> str:
    """HHMMSS for the idle clock. hour12 shows 01-12 instead of 00-23;
    blank_leading_zero (Electro only - its frames are right-justified) drops
    the zero of a single-digit hour so 9:30:00 reads ' 93000', not '093000'."""
    s = now.strftime("%I%M%S" if hour12 else "%H%M%S")
    if blank_leading_zero and s[0] == "0":
        s = s[1:]
    return s


# ------------------------------------------------------------ electro protocol

def electro_frame(payload: str) -> bytes:
    """One display frame: right-justified, space-padded to exactly 6 chars."""
    if len(payload) > ELECTRO_WIDTH:
        raise ValueError(f"payload {payload!r} exceeds {ELECTRO_WIDTH} chars")
    return b"c" + payload.rjust(ELECTRO_WIDTH).encode("ascii") + b"\r\n"


def electro_running_payload(elapsed: float) -> str:
    """MMSSt + trailing space, tenths, capped at 99:59.9 (per ;;TimeRunning)."""
    elapsed = max(0.0, min(elapsed, 99 * 60 + 59.9))
    mm = int(elapsed // 60)
    ss = int(elapsed % 60)
    tenth = int((elapsed * 10) % 10)
    return f"{mm:02d}{ss:02d}{tenth} "


# ------------------------------------------------------------------ discovery

KIND_LABELS = {"ch340": "LED clock", "electro": "Electro clock"}
PROBE_RETRY_SEC = 60.0   # re-ask an unidentified USB serial device this often


def chip_kind(vid: Optional[int], pid: Optional[int]) -> Optional[str]:
    """Clock model implied by the USB serial chip, or None if not a known one."""
    if vid == CH340_VID and pid == CH340_PID:
        return "ch340"
    if vid == PL2303_VID and pid == PL2303_PID:
        return "electro"
    return None


def find_serial_devices(clock_type: str = "auto", port: str = "auto") -> List[Dict]:
    """Every USB serial port as {port, kind, serial, description}.

    kind is the chip-based guess (ch340 / electro) or None for an adapter we
    do not recognise - the service then probes it. Non-USB ports (Bluetooth
    virtual COM ports, motherboard UARTs) are skipped: they are never a clock
    and opening them can stall for seconds.
    Explicit port skips discovery (kind then comes from clock_type,
    defaulting to ch340).
    """
    if port and port != "auto":
        return [{"port": port, "kind": clock_type if clock_type != "auto" else "ch340",
                 "serial": None, "description": "configured"}]
    found = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        kind = chip_kind(p.vid, p.pid)
        if clock_type != "auto" and kind not in (None, clock_type):
            continue
        found.append({"port": p.device, "kind": kind, "serial": p.serial_number or None,
                      "description": p.description or ""})
    return found


def find_clocks(clock_type: str = "auto", port: str = "auto") -> List[Tuple[str, str]]:
    """Known clock adapters only, as (kind, port). See find_serial_devices()."""
    return [(d["kind"], d["port"]) for d in find_serial_devices(clock_type, port) if d["kind"]]


def find_clock(clock_type: str = "auto", port: str = "auto") -> Optional[Tuple[str, str]]:
    """First attached clock as (kind, port), or None. See find_clocks()."""
    found = find_clocks(clock_type, port)
    return found[0] if found else None


def probe_ch340(port: str, wait: float = 0.6) -> bool:
    """Ask an unidentified serial device for a CH340-clock 'R' reply.

    Harmless to anything else: a 4-byte frame at 9600 8N1. Returns True only
    on a checksummed HHMMSS answer. Busy or missing ports return False.
    """
    try:
        ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1,
                            timeout=0.2, write_timeout=1.0)
    except (serial.SerialException, OSError):
        return False
    try:
        ser.reset_input_buffer()
        ser.write(frame("R"))
        ser.flush()
        end, buf = time.time() + wait, bytearray()
        while time.time() < end:
            chunk = ser.read(16)
            if chunk:
                buf += chunk
                if ETX in buf and len(buf) > buf.index(ETX) + 1:
                    break
        return parse_read_response(bytes(buf)) is not None
    except (serial.SerialException, OSError):
        return False
    finally:
        try:
            ser.close()
        except Exception:
            pass


# -------------------------------------------------------------------- drivers

class Ch340Driver:
    """Free-running clock: arm T1 once per gun, set M once for time-of-day,
    then read it back ('R') to make sure it took and stays right.

    Every set is provisional until a read-back agrees with it. Frames sent
    right after the adapter is plugged in can be lost - both directions
    (2026-09-18: the time-of-day sent at connect never reached the clock and
    it showed a stale time, 11 min slow, for the rest of the hour;
    2026-09-19: the set AND the first read-back were lost, so the clock kept
    counting yesterday's race for 33 s until the 30-s spot-check). So a
    silent read-back is retried in VERIFY_AFTER_SET_SEC and the set is sent
    again after UNANSWERED_BEFORE_RESEND silent tries; a wrong read-back
    re-sends at once. A confirmed value is spot-checked every
    DRIFT_CHECK_SEC in both modes.
    """
    kind = "ch340"

    def __init__(self, port: str):
        self.port = port
        self._ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1,
                                  timeout=0.2, write_timeout=1.0)
        self.hour12 = False            # time-of-day in 12-hour form (service sets it)
        self._armed_gun: Optional[datetime] = None
        self._idle_set_at = 0.0
        self._idle_hour12: Optional[bool] = None   # format the clock was last set with
        self._drift_checked_at = time.time()
        self._next_check = 0.0         # when the next read-back is due
        self._unanswered = 0           # consecutive read-backs the clock did not answer
        self._resends = 0              # sets re-sent since the clock last confirmed one

    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass

    def _send(self, pkt: bytes, expect_reply: bool = False, wait: float = 0.5) -> bytes:
        self._ser.reset_input_buffer()
        self._ser.write(pkt)
        self._ser.flush()
        if not expect_reply:
            return b""
        end, buf = time.time() + wait, bytearray()
        while time.time() < end:
            chunk = self._ser.read(16)
            if chunk:
                buf += chunk
                if ETX in buf and len(buf) > buf.index(ETX) + 1:
                    break
        return bytes(buf)

    def read_value(self) -> Optional[int]:
        v = parse_read_response(self._send(frame("R"), expect_reply=True))
        return hhmmss_to_seconds(v) if v else None

    def resync(self):
        """Re-send whatever the clock should show on the next drive(): time
        of day when idle, the count-up when racing. The clock free-runs from
        whatever it was last set to, so after a stepped OS clock (a station
        disciplined over LoRa) or an operator's clock-chip click this beats
        waiting for the hourly refresh or the next spot-check."""
        self._idle_set_at = 0.0
        self._armed_gun = None

    def _check_due(self) -> bool:
        now = time.time()
        return now >= self._next_check or now - self._drift_checked_at > DRIFT_CHECK_SEC

    def _sent(self):
        """A set/arm just went out: read it back soon."""
        self._unanswered = 0
        self._drift_checked_at = time.time()
        self._next_check = self._drift_checked_at + VERIFY_AFTER_SET_SEC

    def _confirmed(self):
        """The read-back agreed: settle down to spot-checks."""
        self._unanswered = 0
        self._resends = 0
        self._drift_checked_at = time.time()
        self._next_check = self._drift_checked_at + DRIFT_CHECK_SEC

    def _resend_level(self) -> int:
        self._resends += 1
        return logging.INFO if self._resends <= RESEND_LOG_LIMIT else logging.DEBUG

    def _verify(self, error_of: Callable[[int], float], resend: Callable[[str, int], None],
                tolerance: float = TOD_TOLERANCE_SEC):
        """Read the clock back and compare with error_of(shown) -> seconds.
        Agreement settles it; disagreement re-sends at once; silence is asked
        again in VERIFY_AFTER_SET_SEC and re-sent after UNANSWERED_BEFORE_RESEND
        silent tries, since the set may not have arrived either."""
        shown = self.read_value()
        if shown is None:
            self._unanswered += 1
            if self._unanswered >= UNANSWERED_BEFORE_RESEND:
                resend(f" - no reply to {self._unanswered} read-backs, re-sent", self._resend_level())
            else:
                self._drift_checked_at = time.time()
                self._next_check = self._drift_checked_at + VERIFY_AFTER_SET_SEC
            return
        err = error_of(shown)
        if err > tolerance:
            resend(f" - clock read {seconds_to_hhmmss(shown)}, {err:.1f} s off, re-set",
                   self._resend_level())
        else:
            self._confirmed()

    @staticmethod
    def _race_error(shown: int, elapsed: float) -> float:
        """How far a whole-second display is from the true race time: zero
        while `elapsed` lies inside the second it shows, else the distance
        to that second - so a correct clock read just before its tick is
        not mistaken for drift."""
        return max(0.0, shown - elapsed, elapsed - (shown + 1))

    def _set_time_of_day(self, reason: str = "", level: int = logging.INFO):
        self._send(frame("M", time_of_day_payload(datetime.now(), self.hour12)))
        self._armed_gun = None
        self._idle_set_at = time.time()
        self._idle_hour12 = self.hour12
        self._sent()
        logger.log(level, f"LED clock (ch340): time-of-day mode ({12 if self.hour12 else 24}-hour){reason}")

    def _time_of_day_error(self, shown: int) -> float:
        """Seconds between what the clock reads and the time of day it should
        show, on the 12- or 24-hour circle so 13:00:00 vs '010000' is 0."""
        expected = hhmmss_to_seconds(time_of_day_payload(datetime.now(), self.hour12))
        period = (12 if self.hour12 else 24) * 3600
        err = abs(shown - expected) % period
        return float(min(err, period - err))

    def drive(self, gun: Optional[datetime], wait: Callable[[float], bool]) -> float:
        if gun is None:
            if self._armed_gun is not None or self._idle_set_at == 0.0 \
                    or self._idle_hour12 != self.hour12 \
                    or time.time() - self._idle_set_at > IDLE_RESYNC_SEC:
                self._set_time_of_day()
            elif self._check_due():
                self._verify(self._time_of_day_error, self._set_time_of_day)
            return 1.0

        moved = self._armed_gun is None or abs((gun - self._armed_gun).total_seconds()) > GUN_MOVED_SEC
        if moved:
            self._arm(gun, wait)
        elif self._check_due():
            self._verify(lambda shown: self._race_error(shown, (datetime.now() - gun).total_seconds()),
                         lambda reason, level: self._arm(gun, wait, reason, level),
                         tolerance=RACE_TOLERANCE_SEC)
        return 1.0

    def _arm(self, gun: datetime, wait: Callable[[float], bool],
             reason: str = "", level: int = logging.INFO):
        """Start the count-up on the next whole-second boundary of race time."""
        elapsed = (datetime.now() - gun).total_seconds()
        target = max(0, int(elapsed) + 1)
        delay = target - elapsed
        if 0 < delay <= 1.2:
            wait(delay)
        self._send(frame("T", "1" + seconds_to_hhmmss(target)))
        self._armed_gun = gun
        self._sent()
        logger.log(level, f"LED clock armed: race elapsed {seconds_to_hhmmss(target)}{reason}")


class ElectroDriver:
    """Remote display: no local timer, we stream every frame it shows."""
    kind = "electro"

    def __init__(self, port: str):
        self.port = port
        self._ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1,
                                  timeout=0.2, write_timeout=2.0,
                                  rtscts=False, dsrdtr=False, xonxoff=False)
        self._ser.write(ELECTRO_INIT)
        self._ser.flush()
        self.hour12 = False            # time-of-day in 12-hour form (service sets it)
        self._last_payload: Optional[str] = None
        self._was_racing = False

    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass

    def _show(self, payload: str):
        if payload != self._last_payload:
            self._ser.write(electro_frame(payload))
            self._ser.flush()
            self._last_payload = payload

    def drive(self, gun: Optional[datetime], wait: Callable[[float], bool]) -> float:
        if gun is None:
            if self._was_racing:
                self._was_racing = False
                logger.info("LED clock (electro): time-of-day mode")
            self._show(time_of_day_payload(datetime.now(), self.hour12, blank_leading_zero=self.hour12))
            return 0.2
        if not self._was_racing:
            self._was_racing = True
            logger.info("LED clock (electro): streaming race time")
        self._show(electro_running_payload((datetime.now() - gun).total_seconds()))
        return 0.1


DRIVER_CLASSES = {"ch340": Ch340Driver, "electro": ElectroDriver}


def create_clock_driver(kind: str, port: str):
    return DRIVER_CLASSES[kind](port)


# -------------------------------------------------------------------- service

class _ClockSlot:
    """One clock we know about, keyed by its current COM port.

    ``entry`` is the persisted config record (label / type / enabled / last
    seen port / USB serial) that follows the clock when the port number
    changes; ``configured`` is True when such a record exists.
    """

    def __init__(self, port: str, kind: Optional[str], entry: Optional[Dict] = None,
                 serial_no: Optional[str] = None):
        self.port = port
        self.kind = kind               # ch340 | electro | None (not identified yet)
        self.entry = entry
        self.serial = serial_no
        self.driver = None
        # adapter plugged in at any point since the console started: a
        # configured clock never seen is simply not used at this meet, one
        # that went away is a problem (console Clocks chip)
        self.seen = False
        self.present = False           # adapter seen in the last discovery pass
        self.error: Optional[str] = None
        self.last_probe = 0.0

    @property
    def present(self) -> bool:
        return self._present

    @present.setter
    def present(self, value: bool):
        self._present = bool(value)
        self.seen = self.seen or self._present

    @property
    def configured(self) -> bool:
        return self.entry is not None

    @property
    def enabled(self) -> bool:
        return bool(self.entry.get("enabled", True)) if self.entry else True

    @property
    def label(self) -> Optional[str]:
        return (self.entry or {}).get("label") or None

    @property
    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.driver:
            return "driving"
        if not self.present:
            return "missing"
        if self.kind is None:
            return "unknown"           # USB serial device we could not identify
        return "busy"                  # adapter is there but the port would not open

    def as_dict(self) -> Dict:
        kind = self.kind or "unknown"
        if self.label:
            label = self.label
        elif self.kind:
            label = f"{KIND_LABELS.get(kind, 'Clock')} {self.port}"
        else:
            label = f"{self.port} (unidentified)"
        return {
            "port": self.port,
            "kind": kind,
            "serial": self.serial,
            "label": label,
            "configured": self.configured,
            "enabled": self.enabled,
            "connected": self.driver is not None,
            "seen": self.seen,
            "status": self.status,
            "error": self.error,
        }


def _norm_found(item) -> Dict:
    """Accept (kind, port) tuples or {port, kind, serial} dicts from finders."""
    if isinstance(item, dict):
        return {"port": item.get("port"), "kind": item.get("kind"), "serial": item.get("serial")}
    kind, port = item
    return {"port": port, "kind": kind, "serial": None}


class LEDClockService:
    """Keeps every attached clock connected and showing the same race time.

    get_gun() supplies the authoritative gun datetime (or None when no race
    is running). Discovery looks at every USB serial port: CH340 and PL2303
    chips are known clock adapters, anything else is asked whether it is a
    CH340-protocol clock, and what is still unidentified is listed so the
    operator can assign a type. Config entries ({label, type, enabled,
    serial, port}) are matched by USB serial number first, then by the
    last-seen port, then by clock type - so a label follows its clock when
    it lands on a different COM number.
    """

    def __init__(self, get_gun: Callable[[], Optional[datetime]],
                 port: str = "auto", clock_type: str = "auto",
                 clocks: Optional[List[Dict]] = None,
                 exclude_ports: Optional[List[str]] = None,
                 on_change: Optional[Callable[[List[Dict]], None]] = None,
                 driver_factory: Callable = create_clock_driver,
                 find_clock_fn: Optional[Callable] = None,
                 find_clocks_fn: Optional[Callable] = None,
                 probe_fn: Optional[Callable[[str], bool]] = None,
                 hour12: bool = False):
        self._get_gun = get_gun
        self._port_pref = port
        self._type_pref = clock_type
        self._hour12 = bool(hour12)
        self._driver_factory = driver_factory
        self._probe = probe_fn if probe_fn is not None else probe_ch340
        if find_clocks_fn is not None:
            self._discover = find_clocks_fn
        elif find_clock_fn is not None:
            # Legacy single-result finder: wrap into a list.
            self._discover = lambda t, p: [r for r in [find_clock_fn(t, p)] if r]
        else:
            self._discover = find_serial_devices
        self._exclude = {str(p).upper() for p in (exclude_ports or []) if p}
        self._on_change = on_change
        self._lock = threading.Lock()
        # The very list object from config - mutated in place so the caller's
        # save persists it.
        self._entries: List[Dict] = clocks if clocks is not None else []
        for e in self._entries:
            if e.get("type") == "auto":
                e.pop("type", None)
        self._slots: Dict[str, _ClockSlot] = {}
        # Configured clocks start as 'missing' slots on their last-seen port.
        for e in self._entries:
            cport = str(e.get("port", "")).strip()
            if cport and e.get("type") != "none":
                self._slots[cport] = _ClockSlot(cport, e.get("type"), e, e.get("serial"))
        self._last_discovery = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ status

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return any(s.driver for s in self._slots.values())

    @property
    def clock_kind(self) -> Optional[str]:
        with self._lock:
            for s in self._slots.values():
                if s.driver:
                    return s.driver.kind
        return None

    @property
    def clocks(self) -> List[Dict]:
        """Per-clock status for the UI: configured first, identified next,
        unidentified serial devices last."""
        with self._lock:
            slots = sorted(self._slots.values(),
                           key=lambda s: (not s.configured, s.kind is None, s.port))
            return [s.as_dict() for s in slots]

    # ------------------------------------------------------------ operator

    @property
    def hour12(self) -> bool:
        return self._hour12

    def set_hour12(self, hour12: bool):
        """Switch the idle time-of-day display between 12- and 24-hour form.
        Live drivers pick it up on their next drive() call."""
        self._hour12 = bool(hour12)

    def resync_time_of_day(self):
        """Make every live clock resend its time-of-day now (after the OS
        clock was stepped). Drivers that stream the time every second
        need nothing and may not implement resync()."""
        with self._lock:
            drivers = [s.driver for s in self._slots.values() if s.driver]
        for driver in drivers:
            resync = getattr(driver, "resync", None)
            if resync:
                try:
                    resync()
                except Exception:
                    logger.exception("LED clock resync failed")

    def _entry_for(self, slot: _ClockSlot, create: bool) -> Optional[Dict]:
        if slot.entry is not None:
            return slot.entry
        if not create:
            return None
        entry = {"port": slot.port}
        if slot.serial:
            entry["serial"] = slot.serial
        if slot.kind:
            entry["type"] = slot.kind
        self._entries.append(entry)
        slot.entry = entry
        return entry

    def update_slot(self, port: str, label: Optional[str] = None,
                    enabled: Optional[bool] = None,
                    kind: Optional[str] = None) -> Optional[str]:
        """Rename / enable / disable / re-type one clock. Returns its kind.

        kind: 'ch340' | 'electro' force the driver, 'auto' forgets a forced
        type, 'none' marks the port as not-a-clock (hidden, never probed).
        The slot gains a config entry so the choice persists and follows
        the adapter.
        """
        changed = False
        with self._lock:
            slot = self._slots.get(port)
            if slot is None:
                slot = _ClockSlot(port, None)
                self._slots[port] = slot
            entry = self._entry_for(slot, create=True)
            if label is not None:
                if label.strip():
                    entry["label"] = label.strip()
                else:
                    entry.pop("label", None)
                changed = True
            if enabled is not None:
                entry["enabled"] = bool(enabled)
                if not entry["enabled"]:
                    self._close_slot(slot)
                changed = True
            if kind is not None:
                if kind == "auto":
                    entry.pop("type", None)
                    slot.kind = None
                else:
                    entry["type"] = kind
                    slot.kind = None if kind == "none" else kind
                self._close_slot(slot)
                if kind == "none":
                    del self._slots[port]
                changed = True
            result = slot.kind
            self._last_discovery = 0.0   # re-discover promptly
        if changed:
            self._notify()
        return result

    def retry(self, port: str):
        """Drop the port (if open), forget a failed probe, rediscover now."""
        with self._lock:
            slot = self._slots.get(port)
            if slot:
                self._close_slot(slot)
                slot.last_probe = 0.0
            self._last_discovery = 0.0

    def _notify(self):
        if self._on_change:
            try:
                self._on_change(self._entries)
            except Exception:
                logger.exception("LED clock config change callback failed")

    # ------------------------------------------------------------ lifecycle

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="led-clock", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._thread = None
        with self._lock:
            for slot in self._slots.values():
                self._close_slot(slot)

    def _close_slot(self, slot: _ClockSlot, error: Optional[str] = None):
        if slot.driver:
            try:
                slot.driver.close()
            except Exception:
                pass
            slot.driver = None
        slot.error = error

    def _disconnect(self):
        """Close every driver (kept for callers of the old single-clock API)."""
        with self._lock:
            for slot in self._slots.values():
                self._close_slot(slot)

    # ------------------------------------------------------------ main loop

    def _loop(self):
        while not self._stop.is_set():
            delay = 1.0
            try:
                need_discovery = any(s.driver is None and s.enabled
                                     for s in self._slots.values()) or not self._slots
                if need_discovery and time.time() - self._last_discovery >= DISCOVER_RETRY_SEC:
                    self._connect()
                delay = self._drive_all()
                if delay is None:
                    # Nothing connected: idle until the next discovery pass.
                    self._stop.wait(DISCOVER_RETRY_SEC)
                    continue
            except Exception:
                logger.exception("LED clock service error")
            self._stop.wait(delay)

    def _drive_all(self) -> Optional[float]:
        """Drive every connected clock once; return the shortest requested
        delay, or None when no clock is connected."""
        gun = self._get_gun()
        delay: Optional[float] = None
        with self._lock:
            active = [s for s in self._slots.values() if s.driver]
        for slot in active:
            try:
                slot.driver.hour12 = self._hour12
                d = slot.driver.drive(gun, self._stop.wait)
            except (serial.SerialException, OSError) as e:
                logger.warning(f"LED clock serial error on {slot.port}, will rediscover: {e}")
                with self._lock:
                    self._close_slot(slot, error=str(e))
                    slot.present = False
                continue
            delay = d if delay is None else min(delay, d)
        return delay

    # ------------------------------------------------------------ discovery

    def _match_entry(self, port: str, kind: Optional[str], serial_no: Optional[str],
                     claimed: set) -> Optional[Dict]:
        """Config entry for a discovered adapter: serial, then last-seen port,
        then an unclaimed entry of the same type whose port is not present."""
        present = {s.port for s in self._slots.values() if s.present}
        if serial_no:
            for e in self._entries:
                if id(e) not in claimed and e.get("serial") == serial_no:
                    return e
        for e in self._entries:
            if id(e) not in claimed and str(e.get("port", "")).upper() == port.upper():
                return e
        if kind:
            for e in self._entries:
                if id(e) in claimed or e.get("type") != kind:
                    continue
                eport = str(e.get("port", ""))
                if not eport or (eport.upper() != port.upper() and eport not in present):
                    return e
        return None

    def _connect(self) -> bool:
        """Discover adapters and open every clock that is not yet driving.

        Returns True if at least one clock is connected afterwards.
        """
        self._last_discovery = time.time()
        found = [_norm_found(f) for f in (self._discover(self._type_pref, self._port_pref) or [])]
        found = [f for f in found if f["port"] and str(f["port"]).upper() not in self._exclude]
        seen_ports = {f["port"] for f in found}
        entries_changed = False
        probe_targets: List[_ClockSlot] = []

        with self._lock:
            # Mark absence first so type-matching can see which ports are live.
            for slot in self._slots.values():
                if slot.port not in seen_ports and slot.driver is None:
                    slot.present = False
            claimed = {id(s.entry) for s in self._slots.values()
                       if s.entry is not None and s.port in seen_ports}
            for f in found:
                port, kind, serial_no = f["port"], f["kind"], f["serial"]
                slot = self._slots.get(port)
                if slot is None or slot.entry is None:
                    entry = self._match_entry(port, kind, serial_no, claimed)
                    if entry is not None:
                        claimed.add(id(entry))
                        if entry.get("type") == "none":
                            if slot is not None:
                                self._close_slot(slot)
                                del self._slots[port]
                            continue
                        # The entry's old 'missing' slot (previous COM number)
                        # is superseded by this live one.
                        for old_port, old in list(self._slots.items()):
                            if old is not slot and old.entry is entry:
                                self._close_slot(old)
                                del self._slots[old_port]
                        if str(entry.get("port", "")) != port:
                            entry["port"] = port
                            entries_changed = True
                        if serial_no and entry.get("serial") != serial_no:
                            entry["serial"] = serial_no
                            entries_changed = True
                        if slot is None:
                            slot = _ClockSlot(port, None, entry, serial_no)
                            self._slots[port] = slot
                        else:
                            slot.entry = entry
                    elif slot is None:
                        slot = _ClockSlot(port, None, None, serial_no)
                        self._slots[port] = slot
                if slot.entry is not None and slot.entry.get("type") not in (None, "none"):
                    slot.kind = slot.entry["type"]          # operator's choice wins
                elif slot.kind is None:
                    slot.kind = kind                         # chip-based guess (may be None)
                slot.serial = slot.serial or serial_no
                slot.present = True
                if slot.kind is None and slot.enabled and slot.driver is None \
                        and time.time() - slot.last_probe >= PROBE_RETRY_SEC:
                    probe_targets.append(slot)
            # Forget auto-discovered devices whose adapter has gone away.
            for port in [p for p, s in self._slots.items()
                         if not s.configured and not s.present and s.driver is None]:
                del self._slots[port]

        # Identify unknown USB serial devices by asking for a CH340 'R' reply.
        for slot in probe_targets:
            slot.last_probe = time.time()
            try:
                is_ch340 = bool(self._probe(slot.port))
            except Exception as e:
                logger.debug(f"LED clock probe failed on {slot.port}: {e}")
                is_ch340 = False
            if is_ch340:
                with self._lock:
                    slot.kind = "ch340"
                logger.info(f"LED clock identified by probe: ch340 on {slot.port}")

        with self._lock:
            targets = [s for s in self._slots.values()
                       if s.driver is None and s.enabled and s.present and s.kind]

        for slot in targets:
            try:
                driver = self._driver_factory(slot.kind, slot.port)
            except (serial.SerialException, OSError) as e:
                logger.debug(f"LED clock open failed on {slot.port}: {e}")
                with self._lock:
                    slot.error = str(e)
                    if slot.configured and slot.port not in seen_ports:
                        # Explicit port on an adapter discovery does not list:
                        # "access denied" means it exists but something else
                        # (FinishLynx?) holds it; anything else means absent.
                        slot.present = "denied" in str(e).lower()
                continue
            with self._lock:
                slot.driver = driver
                slot.kind = getattr(driver, "kind", slot.kind)
                slot.present = True
                slot.error = None
            logger.info(f"LED clock connected: {slot.kind} on {slot.port}"
                        + (f" ({slot.label})" if slot.label else ""))

        if entries_changed:
            self._notify()
        return self.is_connected
