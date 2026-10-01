"""FinishLynx's Lynx.cfg: check it (console Setup check) and, on request,
set the three things the middleware needs (installer's optional step).

    LapTime       an entry with Device "Lynx" (Type 11) listening on TCP 5000
                  - the middleware dials it with S/T packets. Ipico (Type 9)
                  on that port silently drops them (2026-09-12).
    Scoreboard    an entry with script Example2.lss (or Example.lss) sending
                  UDP to 127.0.0.1:5891 with running time on - event/heat and
                  the gun come back to the console through it.
    Folders       input (DataInDir) = <results>\\RFID, where MeetPro writes the
                  start lists; output (DataOutDir) and the event directory =
                  <results>, where FinishLynx saves its .lif files.

File format, one key per line (CRLF, ANSI):
    \\LapTime\\3\\Type:Int,1,,=11
    <key path>:<type header>=<value>
Entries are numbered sub-keys (\\Scoreboards\\1\\..., \\LapTime\\1\\...).
Line 2 holds binary FinishLynx writes itself; every line we do not touch is
written back byte for byte, and apply() keeps a backup next to the file.

    python -m rfid_finishlynx.lynx_cfg check [--cfg PATH] [--results DIR]
    python -m rfid_finishlynx.lynx_cfg apply [--cfg PATH] [--results DIR]
"""

import argparse
import ntpath
import os
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

ENCODING = "latin-1"          # byte-transparent: the file is ANSI plus one binary line
DEFAULT_RESULTS = r"C:\Finish_Lynx_Results"
RFID_SUBDIR = "RFID"
LAPTIME_PORT = 5000
SCOREBOARD_PORT = 5891
LAPTIME_TYPE_LYNX = 11
LAPTIME_TYPE_IPICO = 9
SCRIPTS = ("Example2.lss", "Example.lss")   # start-list format the console parses

CFG_CANDIDATES = (
    r"C:\Lynx\Lynx.cfg",
    r"C:\FinishLynx\Lynx.cfg",
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Lynx", "Lynx.cfg"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Lynx", "Lynx.cfg"),
)

LINE_RE = re.compile(r"^(\\[^:]*):([^=]*)=(.*)$")

# A complete scoreboard entry (FinishLynx 12.10, this rig's working
# "Middleware" entry) for a Lynx.cfg that has none to copy from.
SCOREBOARD_TEMPLATE = [
    ("Name", "String,1", "Middleware"), ("Script", "String,1", "Example2.lss"),
    ("SerialPort", "String,1", f"-3,127.0.0.1,{SCOREBOARD_PORT}"),
    ("CharMode", "Int,1,,", "0"), ("MinUpdateDelay", "Int,1,,", "0"), ("UpdateHold", "Int,1,,", "0"),
    ("TimeMode", "Int,1,,", "1"), ("ResultsIfArmed", "Int,1,,", "0"), ("IgnoreRun", "Int,1,,", "0"),
    ("IgnorePause", "Int,1,,", "0"), ("IgnoreFinish", "Int,1,,", "0"), ("IgnoreStop", "Int,1,,", "0"),
    ("PauseToFinish", "Int,1,,", "0"), ("FinishToPause", "Int,1,,", "0"), ("TimeOffset", "Int,1,,", "0"),
    ("TimeAdjust", "Int,1,,", "0"), ("TimePause", "Int,1,,", "3000"), ("AutoBreak", "Int,1,,", "0"),
    ("AutoBreakCapt", "Int,1,,", "0"), ("TimeNoDelay", "Int,1,,", "1"), ("ResultsMode", "Int,1,,", "0"),
    ("SendPlace", "Int,1,,", "0"), ("PageMode", "Int,1,,", "1"), ("PageSize", "Int,1,,", "1"),
    ("PageMax", "Int,1,,", "-1"), ("PageMask", "Int,1,,", "0"), ("PageTime", "Int,1,,", "5000"),
    ("IncludeFirst", "Bool,1", "0"), ("AffilAbbr", "Bool,1", "0"), ("TimePrecision", "Int,1,,", "0"),
    ("SuppressMinute", "Int,1,,", "0"), ("SuppressCutoff", "Int,1,,", "100000"), ("SortBy", "Int,1,,", "0"),
]
LAPTIME_TEMPLATE = [
    ("Name", "String,1", "RFID Middleware"), ("Type", "Int,1,,", str(LAPTIME_TYPE_LYNX)),
    ("SerialPort", "String,1", f"-2,0.0.0.0,{LAPTIME_PORT}"), ("LPAddress", "Int,1,,", "0"),
    ("ActiveSplitsOn", "Int,1,,", "0"), ("ActiveSplits", "String,1", ""), ("TimeOffset", "Int,1,,", "0"),
    ("SyncSource", "Int,1,,", "1"), ("Token", "Int,1,,", "0"),
]


def find_cfg(explicit: Optional[str] = None) -> Optional[str]:
    """Path of Lynx.cfg: the explicit one, else the usual install folders."""
    for p in ([explicit] if explicit else CFG_CANDIDATES):
        if p and os.path.isfile(p):
            return p
    return None


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path.strip())) if path and path.strip() else ""


def _port_of(serial_port: str) -> Tuple[Optional[int], str, Optional[int]]:
    """'-3,127.0.0.1,5891' -> (-3, '127.0.0.1', 5891); serial ports -> (n, '', None)."""
    parts = [p.strip() for p in (serial_port or "").split(",")]
    try:
        kind = int(parts[0])
    except (ValueError, IndexError):
        return None, "", None
    if kind < 0 and len(parts) >= 3:
        try:
            return kind, parts[1], int(parts[2])
        except ValueError:
            return kind, parts[1], None
    return kind, "", None


class LynxCfg:
    """Lines of Lynx.cfg with keyed access; untouched lines stay byte-exact."""

    def __init__(self, text: str):
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.trailing = text.endswith(self.newline)
        self.lines: List[str] = text.split(self.newline)
        if self.trailing:
            self.lines.pop()
        self.changes: List[str] = []

    @classmethod
    def load(cls, path: str) -> "LynxCfg":
        with open(path, "rb") as f:
            return cls(f.read().decode(ENCODING))

    def dump(self) -> str:
        return self.newline.join(self.lines) + (self.newline if self.trailing else "")

    def save(self, path: str):
        with open(path, "wb") as f:
            f.write(self.dump().encode(ENCODING))

    # ------------------------------------------------------------ access

    def _find(self, key: str) -> Optional[int]:
        prefix = key + ":"
        for i, line in enumerate(self.lines):
            if line.startswith(prefix):
                return i
        return None

    def get(self, key: str) -> Optional[str]:
        i = self._find(key)
        if i is None:
            return None
        m = LINE_RE.match(self.lines[i])
        return m.group(3) if m else None

    def has_section(self, section: str) -> bool:
        return self._find(f"\\{section}\\") is not None

    def set(self, key: str, value: str, header: str = "String,1") -> bool:
        """Set a value (keeping the line's own type header). Returns True if
        something changed. A missing key is appended after its parent's
        last line."""
        i = self._find(key)
        if i is not None:
            m = LINE_RE.match(self.lines[i])
            if m and m.group(3) == value:
                return False
            hdr = m.group(2) if m else header
            old = m.group(3) if m else ""
            self.lines[i] = f"{key}:{hdr}={value}"
            self.changes.append(f"{key}: {old!r} -> {value!r}")
            return True
        parent = key.rsplit("\\", 1)[0] + "\\"
        at = self._block_end(parent)
        if at is None:
            self.lines.append(f"{parent}:,1=")
            at = len(self.lines)
        self.lines.insert(at, f"{key}:{header}={value}")
        self.changes.append(f"{key}: added {value!r}")
        return True

    def _block_end(self, prefix: str) -> Optional[int]:
        """Index just past the last line under `prefix`, or None."""
        last = None
        for i, line in enumerate(self.lines):
            if line.startswith(prefix):
                last = i
        return None if last is None else last + 1

    def entries(self, section: str) -> Dict[int, Dict[str, str]]:
        """{n: {field: value}} for \\<section>\\<n>\\<field> lines."""
        out: Dict[int, Dict[str, str]] = {}
        rx = re.compile(r"^\\" + re.escape(section) + r"\\(\d+)\\([^\\:]+):([^=]*)=(.*)$")
        for line in self.lines:
            m = rx.match(line)
            if m:
                out.setdefault(int(m.group(1)), {})[m.group(2)] = m.group(4)
        return out

    def add_entry(self, section: str, fields: List[Tuple[str, str, str]]) -> int:
        """Append entry max+1 of `section` after the section's last line."""
        n = max(self.entries(section) or {0: None}) + 1
        at = self._block_end(f"\\{section}\\")
        block = [f"\\{section}\\{n}\\:,1="] + [f"\\{section}\\{n}\\{k}:{h}={v}" for k, h, v in fields]
        if at is None:
            block.insert(0, f"\\{section}\\:,1=")
            at = len(self.lines)
        self.lines[at:at] = block
        name = next((v for k, _, v in fields if k == "Name"), "")
        self.changes.append(f"\\{section}\\{n}: added entry {name!r}")
        return n

    def entry_fields(self, section: str, n: int) -> List[Tuple[str, str, str]]:
        """(field, header, value) of one entry, in file order - a template."""
        rx = re.compile(r"^\\" + re.escape(section) + r"\\" + str(n) + r"\\([^\\:]+):([^=]*)=(.*)$")
        return [(m.group(1), m.group(2), m.group(3)) for m in map(rx.match, self.lines) if m]


# ------------------------------------------------------------------ checks

def _laptime_state(cfg: LynxCfg):
    """(ok, entry_no, problem) for the LapTime side."""
    if not cfg.has_section("LapTime"):
        return False, None, "the LapTime plug-in is not installed (no LapTime section)"
    on_port = []
    for n, f in sorted(cfg.entries("LapTime").items()):
        kind, _host, port = _port_of(f.get("SerialPort", ""))
        if kind is not None and kind < 0 and port == LAPTIME_PORT:
            on_port.append((n, f))
    for n, f in on_port:
        if f.get("Type") == str(LAPTIME_TYPE_LYNX):
            return True, n, ""
    if on_port:
        n, f = on_port[0]
        why = " (Ipico - it silently drops the console's packets)" if f.get("Type") == str(LAPTIME_TYPE_IPICO) else ""
        return False, n, f"entry {n} \"{f.get('Name', '')}\" on port {LAPTIME_PORT} has Device type {f.get('Type')}{why}; it must be Lynx"
    return False, None, f"no LapTime entry with Device \"Lynx\" on network port {LAPTIME_PORT}"


def _scoreboard_state(cfg: LynxCfg):
    candidates = []
    for n, f in sorted(cfg.entries("Scoreboards").items()):
        kind, host, port = _port_of(f.get("SerialPort", ""))
        if kind == -3 and port == SCOREBOARD_PORT:
            candidates.append((n, f, host))
    for n, f, host in candidates:
        script_ok = f.get("Script", "").lower() in {s.lower() for s in SCRIPTS}
        if script_ok and f.get("TimeMode", "1") != "0":
            return True, n, ""
    if candidates:
        n, f, _ = candidates[0]
        if f.get("Script", "").lower() not in {s.lower() for s in SCRIPTS}:
            return False, n, f"entry {n} \"{f.get('Name', '')}\" sends to port {SCOREBOARD_PORT} with script {f.get('Script')}; use Example2.lss"
        return False, n, f"entry {n} \"{f.get('Name', '')}\" has running time off - the console will not see guns"
    return False, None, f"no scoreboard entry sending UDP to 127.0.0.1:{SCOREBOARD_PORT} (script Example2.lss)"


def check(cfg: LynxCfg, results_dir: str = DEFAULT_RESULTS) -> List[Dict]:
    """One row per setting: {id, ok, label, detail}."""
    rows = []
    ok, n, why = _laptime_state(cfg)
    rows.append({"id": "fl_laptime", "ok": ok, "label": f"LapTime: Device Lynx, network port {LAPTIME_PORT}",
                 "detail": f"entry {n}" if ok else why})
    ok, n, why = _scoreboard_state(cfg)
    rows.append({"id": "fl_scoreboard", "ok": ok,
                 "label": f"Scoreboard: Example2.lss, UDP 127.0.0.1:{SCOREBOARD_PORT}, running time",
                 "detail": f"entry {n}" if ok else why})
    rfid = ntpath.join(results_dir, RFID_SUBDIR)
    din = cfg.get(r"\Database\File\DataInDir") or ""
    rows.append({"id": "fl_input", "ok": _norm(din) == _norm(rfid), "label": f"Input folder = {rfid}",
                 "detail": f"now {din or '(not set)'}"})
    dout = cfg.get(r"\Database\File\DataOutDir") or ""
    evdir = cfg.get(r"\Event\Directory") or ""
    out_ok = _norm(dout) == _norm(results_dir) and (not evdir or _norm(evdir) == _norm(results_dir))
    rows.append({"id": "fl_output", "ok": out_ok, "label": f"Output + event folder = {results_dir}",
                 "detail": f"output {dout or '(not set)'}, event {evdir or '(not set)'}"})
    return rows


def apply(cfg: LynxCfg, results_dir: str = DEFAULT_RESULTS, lynx_dir: Optional[str] = None) -> List[str]:
    """Make check() pass. Returns what changed (empty = already right)."""
    cfg.changes = []
    ok, n, _ = _laptime_state(cfg)
    if not ok and cfg.has_section("LapTime"):
        if n is not None:
            cfg.set(f"\\LapTime\\{n}\\Type", str(LAPTIME_TYPE_LYNX), "Int,1,,")
        else:
            fields = [(k, h, str(random.randint(10_000_000, 99_999_999)) if k == "Token" else v)
                      for k, h, v in LAPTIME_TEMPLATE]
            cfg.add_entry("LapTime", fields)
    ok, n, _ = _scoreboard_state(cfg)
    if not ok:
        script = SCRIPTS[0]
        if lynx_dir and not os.path.isfile(os.path.join(lynx_dir, script)) \
                and os.path.isfile(os.path.join(lynx_dir, SCRIPTS[1])):
            script = SCRIPTS[1]
        if n is not None:
            cfg.set(f"\\Scoreboards\\{n}\\Script", script)
            cfg.set(f"\\Scoreboards\\{n}\\TimeMode", "1", "Int,1,,")
        else:
            existing = cfg.entries("Scoreboards")
            fields = cfg.entry_fields("Scoreboards", max(existing)) if existing else list(SCOREBOARD_TEMPLATE)
            want = {"Name": "Middleware", "Script": script, "SerialPort": f"-3,127.0.0.1,{SCOREBOARD_PORT}",
                    "TimeMode": "1", "ResultsMode": "0", "ResultsIfArmed": "0"}
            fields = [(k, h, want.get(k, v)) for k, h, v in fields]
            have = {k for k, _, _ in fields}
            fields += [(k, h, v) for k, h, v in SCOREBOARD_TEMPLATE if k not in have]
            cfg.add_entry("Scoreboards", fields)
    cfg.set(r"\Database\File\DataInDir", ntpath.join(results_dir, RFID_SUBDIR))
    cfg.set(r"\Database\File\DataOutDir", results_dir)
    cfg.set(r"\Event\Directory", results_dir)
    return list(cfg.changes)


def finishlynx_running() -> bool:
    """Lynx.exe in the task list (FinishLynx rewrites Lynx.cfg on exit)."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Lynx.exe", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "lynx.exe" in out.lower()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=("check", "apply"))
    ap.add_argument("--cfg", help="path of Lynx.cfg (default: find it)")
    ap.add_argument("--results", default=DEFAULT_RESULTS, help="FinishLynx results folder")
    a = ap.parse_args(argv)
    path = find_cfg(a.cfg)
    if not path:
        print("Lynx.cfg not found - is FinishLynx installed? (looked in "
              + ", ".join(CFG_CANDIDATES) + ")")
        return 2
    cfg = LynxCfg.load(path)
    if a.action == "check":
        rows = check(cfg, a.results)
        for r in rows:
            print(f"[{'OK ' if r['ok'] else 'FIX'}] {r['label']} - {r['detail']}")
        return 0 if all(r["ok"] for r in rows) else 1
    if finishlynx_running():
        print("FinishLynx is running. Close it first - it rewrites Lynx.cfg when it exits.")
        return 3
    changes = apply(cfg, a.results, os.path.dirname(path))
    if not changes:
        print(f"{path}: already set up, nothing changed.")
        return 0
    backup = f"{path}.before-rfid-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, backup)
    cfg.save(path)
    for d in (a.results, ntpath.join(a.results, RFID_SUBDIR)):
        os.makedirs(d, exist_ok=True)
    print(f"{path} updated (backup: {backup}):")
    for c in changes:
        print("  " + c)
    print("Open FinishLynx and check Options -> LapTime and Scoreboard. To undo, copy the backup back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
