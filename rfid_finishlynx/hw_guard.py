"""Hardware ownership guard.

Only ONE middleware process per machine may touch the hardware: the RFID
reader, the LED clocks' COM ports, the LoRa radio, the camera sniffer and
FinishLynx's LapTime socket. Any other process - a staging copy started to
preview a change, an end-to-end harness, an accidental double launch -
must come up in PREVIEW mode: web UI only, no device is opened.

Why this exists (2026-09-19, during a race): a "safe" test instance whose
reader was pointed at 127.0.0.1 still took the live finish reader, because
the reader driver falls back to the other vendor and then to the reader's
known addresses. Configuration alone cannot make a second instance safe;
this lock can.

Three independent ways to land in preview mode, any one is enough:
  * the environment variable RFID_PREVIEW=1 (or --preview on the command line)
  * a file named PREVIEW_ONLY in the working directory or next to the
    config file - the staging copy carries one permanently
  * another process on this machine already holds the hardware lock

The lock is an OS-level exclusive lock on a small file, released
automatically when the owning process exits, so a crash never leaves a
stale lock behind.
"""

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

PREVIEW_ENV = "RFID_PREVIEW"
LOCK_PATH_ENV = "RFID_HW_LOCK"
MARKER_NAME = "PREVIEW_ONLY"
# Windows locks a byte range and refuses reads that overlap it, so the lock
# lives far past the end of the file and the owner's PID at offset 0 stays
# readable by the process that was refused.
LOCK_OFFSET = 1 << 20


def default_lock_path() -> Path:
    override = os.environ.get(LOCK_PATH_ENV)
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "rfid_finishlynx"
    else:
        base = Path(tempfile.gettempdir())
    return base / "rfid_finishlynx-hardware.lock"


def preview_requested(config_path: Optional[str] = None) -> Optional[str]:
    """The reason this process must not touch hardware, or None."""
    if os.environ.get(PREVIEW_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return f"{PREVIEW_ENV}=1 in the environment"
    candidates = [Path.cwd() / MARKER_NAME]
    if config_path:
        candidates.append(Path(config_path).resolve().parent / MARKER_NAME)
    for marker in candidates:
        if marker.exists():
            return f"{MARKER_NAME} file present in {marker.parent}"
    return None


class HardwareLock:
    """Exclusive, non-blocking, process-lifetime lock on a file."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_lock_path()
        self._fh = None
        self.owner_pid: Optional[int] = None    # who holds it when acquire() fails

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                fh.seek(LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_pid = self._read_pid(fh)
            fh.close()
            return False
        # Ours. Record who we are for the next process that is refused.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n".encode("ascii"))
            fh.flush()
        except OSError:
            pass
        self._fh = fh
        self.owner_pid = os.getpid()
        return True

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                fh.seek(LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()

    @staticmethod
    def _read_pid(fh) -> Optional[int]:
        try:
            fh.seek(0)
            text = fh.read(64).decode("ascii", "replace").strip()
            return int(text.split()[0]) if text else None
        except (OSError, ValueError):
            return None

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass
