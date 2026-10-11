"""Setup check: is this PC ready to time a meet? One row per thing a new
timer has to get right, each with how to fix it, so the console can say
what is missing without anyone reading the code (2026-09-30).

Row: {group, id, label, ok, detail, fix}
    ok True = fine, False = needs fixing, None = does not apply / nothing to
    judge yet (e.g. no clock plugged in, MeetPro on another PC).

Everything here only READS: Lynx.cfg, MeetPro's preferences, the driver
store, the live status, the open meet's last start-list write. Fixing is the installer's / the operator's job.
"""

import ipaddress
import json
import logging
import ntpath
import os
import socket
import subprocess
import time
from typing import Dict, List, Optional

from . import lynx_cfg

logger = logging.getLogger(__name__)

MEETPRO_PREFS = os.path.join(os.environ.get("APPDATA", ""), "com.directathletics.MeetPro2",
                             "Local Store", "preferences.json")
DRIVER_INFS = {"ch340": ("ch341ser.inf",), "pl2303": ("ser2pl.inf", "ser2pl_1.inf")}
_DRIVER_CACHE: Dict[str, object] = {"at": 0.0, "have": None}
DRIVER_CACHE_SEC = 300.0


def _row(group, id_, label, ok, detail="", fix=""):
    return {"group": group, "id": id_, "label": label, "ok": ok, "detail": detail,
            "fix": fix if ok is False else ""}


def local_ipv4() -> List[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def same_subnet(ip: str, host: str, prefix: int = 24) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(f"{host}/{prefix}", strict=False)
    except ValueError:
        return False


def installed_driver_infs() -> Optional[set]:
    """Original .inf names in the Windows driver store (cached), or None
    when that cannot be read (not Windows, pnputil missing)."""
    if time.time() - float(_DRIVER_CACHE["at"]) < DRIVER_CACHE_SEC:
        return _DRIVER_CACHE["have"]
    have = None
    if os.name == "nt":
        try:
            out = subprocess.run(["pnputil", "/enum-drivers"], capture_output=True, text=True,
                                 timeout=20, errors="replace").stdout
            # every "...: name.inf" value (published and original names);
            # the labels themselves are translated on non-English Windows
            have = {line.split(":", 1)[1].strip().lower() for line in out.splitlines()
                    if ":" in line and line.strip().lower().endswith(".inf")}
        except (OSError, subprocess.SubprocessError):
            have = None
    _DRIVER_CACHE.update(at=time.time(), have=have)
    return have


def meetpro_fat_directory(path: Optional[str] = None) -> Optional[str]:
    """MeetPro's Interfaces -> F.A.T. -> Directory on this PC, or None."""
    path = path or MEETPRO_PREFS
    try:
        with open(path, encoding="utf-8") as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        return None
    fat = prefs.get("fat") if isinstance(prefs, dict) else None
    return fat.get("directory") if isinstance(fat, dict) else None


def start_lists_at(folder: str) -> Optional[float]:
    """Newest mtime of lynx.evt / lynx.ppl in `folder` (any case), or None."""
    try:
        names = [n for n in os.listdir(folder) if n.lower() in ("lynx.evt", "lynx.ppl")]
    except OSError:
        return None
    times = [os.path.getmtime(os.path.join(folder, n)) for n in names]
    return max(times) if times else None


def meetpro_row(results: str, rfid: str) -> Dict:
    """Does MeetPro write the start lists into the RFID folder? MeetPro often
    runs on another laptop through a share, so where its NEWEST start lists
    land is the evidence; its preferences on this PC are only a fallback
    (on this rig they were a year stale, 2026-09-30). Only asked with no
    meet open on the Meet tab; an open meet writes its own (meet_start_lists_row)."""
    label = "MeetPro writes the start lists into the RFID folder"
    fix = f"MeetPro: Interfaces -> F.A.T. -> Setup -> Directory = {rfid} (from another laptop: the same folder over the share)"
    in_rfid, in_results = start_lists_at(rfid), start_lists_at(results)
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))  # noqa: E731
    if in_rfid and (not in_results or in_rfid >= in_results):
        return _row("Folders", "meetpro_dir", label, True, f"newest start lists {stamp(in_rfid)}")
    if in_results:
        return _row("Folders", "meetpro_dir", label, False,
                    f"newest start lists are in {results} ({stamp(in_results)})", fix)
    mp = meetpro_fat_directory()
    if mp is None:
        return _row("Folders", "meetpro_dir", label, None,
                    "no start lists yet - open or import a meet on the Meet tab, "
                    "or export them from MeetPro to F.A.T.")
    return _row("Folders", "meetpro_dir", label, lynx_cfg._norm(mp) == lynx_cfg._norm(rfid),
                f"no start lists yet; MeetPro on this PC is set to {mp}", fix)


def meet_start_lists_row(sl: Dict, rfid: str) -> Dict:
    """An open meet writes lynx.evt/.ppl/.sch itself on every change
    (meet_service), so judge that write, not MeetPro (2026-10-05: the
    MeetPro row went green on the console's own files and named MeetPro
    to an operator who never uses it)."""
    label = f"Start lists from the open meet into the RFID folder ({rfid})"
    errors, skipped = sl.get("errors") or [], sl.get("skipped")
    if errors:
        return _row("Folders", "meet_start_lists", label, False, f"not written: {errors[0]}",
                    f"{rfid} must exist and not be read-only; the Meet tab's Info page shows the "
                    "error. Any change to the meet writes them again.")
    if skipped == "no folder":
        return _row("Folders", "meet_start_lists", label, False, "not written: no results folder set",
                    "Meet tab -> the Lynx folder button -> the FinishLynx results folder (C:\\Finish_Lynx_Results).")
    if skipped == "file only":
        return _row("Folders", "meet_start_lists", label, None,
                    "meet opened without FinishLynx: start lists are not written")
    if skipped == "preview":
        return _row("Folders", "meet_start_lists", label, None, "preview copy: never written")
    if skipped == "empty meet":
        return _row("Folders", "meet_start_lists", label, None,
                    "nothing to write yet - import the entries on the Meet tab")
    if not sl.get("written_at"):
        # nothing written since the console started: the files from before
        # the restart are still FinishLynx's start lists
        on_disk = start_lists_at(rfid)
        if on_disk:
            return _row("Folders", "meet_start_lists", label, True,
                        "in the folder since " + time.strftime("%Y-%m-%d %H:%M", time.localtime(on_disk)))
        return _row("Folders", "meet_start_lists", label, None,
                    "not written yet - any change to the meet writes them")
    when = str(sl["written_at"]).replace("T", " ")[:16]
    warns = sl.get("warnings") or []
    return _row("Folders", "meet_start_lists", label, True,
                f"written {when}" + (f" ({len(warns)} warning{'s' if len(warns) != 1 else ''})" if warns else ""))


def bib_check_row(chk: Dict) -> Dict:
    """The bib check before a race (meet/bibcheck.py, 2026-10-10): the bibs
    handed out, the meet's bibs and FinishLynx's lynx.ppl agree."""
    label = "Bibs match what was handed out and FinishLynx's start list"
    fix = "Meet tab -> Info -> Bib check: put the printed numbers back, keep the new ones, or write the start lists again."
    lx = chk.get("lynx") or {}
    bad = len(chk.get("changed") or []) + len(lx.get("wrong") or []) + len(lx.get("missing") or [])
    if bad:
        bits = []
        if chk.get("changed"):
            bits.append(f"{len(chk['changed'])} runner(s) with another bib than printed")
        if lx.get("wrong"):
            bits.append(f"{len(lx['wrong'])} bib(s) named differently in FinishLynx")
        if lx.get("missing"):
            bits.append(f"{len(lx['missing'])} bib(s) not in FinishLynx's start list")
        return _row("FinishLynx", "bib_check", label, False, "; ".join(bits), fix)
    if not chk.get("recorded"):
        return _row("FinishLynx", "bib_check", label, None,
                    "no bibs handed out yet (printing the rosters or bib labels records them)"
                    + ("" if lx.get("checked") else "; no lynx.ppl to compare"))
    return _row("FinishLynx", "bib_check", label, True,
                f"{chk['recorded']} of {chk['runners']} runners' bibs as handed out"
                + (", FinishLynx's start list agrees" if lx.get("checked") else " (no lynx.ppl to compare)"))


def main(argv=None) -> int:
    """INSTALL.bat's questions, before the console has ever run:
        --network [HOST]   exit 1 if no adapter is on HOST's /24 (reader)
        --wireshark        exit 1 if tshark (Wireshark + Npcap) is missing"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", nargs="?", const="192.168.0.128")
    ap.add_argument("--wireshark", action="store_true")
    a = ap.parse_args(argv)
    if a.network:
        ips = local_ipv4()
        ok = any(same_subnet(ip, a.network) for ip in ips)
        net = a.network.rsplit(".", 1)[0]
        print(f"  This PC's addresses: {', '.join(ips) or 'none'}")
        print(f"  {'OK - on' if ok else 'NOT on'} the reader's network ({net}.x, reader at {a.network})")
        return 0 if ok else 1
    if a.wireshark:
        from .camera_start import find_tshark
        t = find_tshark()
        print(f"  Wireshark: {t or 'not installed'}")
        return 0 if t else 1
    ap.print_help()
    return 2


def run(app) -> List[Dict]:
    cfg = app.config
    status = app.get_status()
    rows: List[Dict] = []
    rc = cfg.reader
    reader = getattr(app, "reader", None)

    # ------------------------------------------------------------ reader
    ips = local_ipv4()
    on_net = any(same_subnet(ip, rc.host) for ip in ips)
    rows.append(_row("Reader", "reader_net", f"This PC on the reader's network ({rc.host.rsplit('.', 1)[0]}.x)",
                     on_net, ", ".join(ips) or "no network address",
                     f"Give the wired network adapter a fixed address on {rc.host.rsplit('.', 1)[0]}.x "
                     f"(e.g. {rc.host.rsplit('.', 1)[0]}.200, mask 255.255.255.0) - INSTALL.bat offers to."))
    up = bool(status.get("reader_connected"))
    rows.append(_row("Reader", "reader_up", f"Reader connected ({rc.type} at {rc.host})", up,
                     "reading" if up else "not answering",
                     "Reader powered (12 V brick or PoE) and its network cable in the same switch as this PC?"))
    if rc.type == "vanch":
        active = getattr(reader, "active_antennas", None) if up else None
        ports = getattr(reader, "port_count", None)
        want_all = not rc.antennas
        if active is None:
            ok, detail = None, "waiting for the reader"
        elif want_all:
            ok = bool(ports) and sorted(active) == list(range(1, ports + 1))
            detail = f"polling {', '.join(map(str, active))} of {ports or '?'} ports"
        else:
            ok = sorted(active) == sorted(rc.antennas)
            detail = f"polling {', '.join(map(str, active))} (set: {', '.join(map(str, rc.antennas))})"
        rows.append(_row("Reader", "reader_antennas", "Every antenna port polled", ok, detail,
                         "Stations chip -> This PC -> Reader...: tick every port (or 'every port')."))
        beep = getattr(reader, "buzzer_on", None) if up else None
        want = bool(getattr(rc, "beep", False))
        rows.append(_row("Reader", "reader_beep", "Reader beep " + ("on" if want else "off"),
                         None if beep is None else beep == want,
                         "unknown" if beep is None else ("beeping" if beep else "quiet"),
                         "The console sets it on every connect: restart the reader from the Stations chip."))

    # ------------------------------------------------------------ clocks
    have = installed_driver_infs()
    if have is None:
        rows.append(_row("Clocks", "clock_drivers", "LED clock USB drivers (CH340, PL2303)", None,
                         "could not read the driver list"))
    else:
        missing = [k.upper() for k, infs in DRIVER_INFS.items() if not any(i in have for i in infs)]
        rows.append(_row("Clocks", "clock_drivers", "LED clock USB drivers (CH340, PL2303)", not missing,
                         "installed" if not missing else "missing: " + ", ".join(missing),
                         r"Right-click C:\rfid_finishlynx\deploy\install-clock-drivers.bat -> Run as administrator."))
    clocks = [c for c in status.get("led_clocks") or [] if c.get("status") != "unknown"]
    driving = [c for c in clocks if c.get("status") == "driving"]
    if not cfg.led_clock.enabled:
        rows.append(_row("Clocks", "clocks", "LED clocks", None, "turned off in settings"))
    else:
        rows.append(_row("Clocks", "clocks", "LED clocks", True if driving else None,
                         ", ".join(f"{c.get('label')} ({c.get('port')})" for c in driving) or "none plugged in",
                         ""))

    # ------------------------------------------------------------ FinishLynx
    results = cfg.event_directory or lynx_cfg.DEFAULT_RESULTS
    path = lynx_cfg.find_cfg()
    if not path:
        rows.append(_row("FinishLynx", "fl_installed", "FinishLynx installed", False, "Lynx.cfg not found",
                         "Install FinishLynx (with the LapTime plug-in) on this PC."))
    else:
        try:
            for r in lynx_cfg.check(lynx_cfg.LynxCfg.load(path), results):
                rows.append(_row("FinishLynx", r["id"], r["label"], r["ok"], r["detail"],
                                 "Close FinishLynx, then run C:\\rfid_finishlynx\\deploy\\configure-finishlynx.bat "
                                 "(or set it by hand - Quick Start guide, FinishLynx section)."))
        except OSError as e:
            rows.append(_row("FinishLynx", "fl_cfg", "FinishLynx settings readable", False, str(e)))
    fl_up = bool(status.get("finishlynx_connected"))
    rows.append(_row("FinishLynx", "fl_link", "LapTime link to FinishLynx (TCP 5000)", fl_up,
                     "connected" if fl_up else "not connected",
                     "Open FinishLynx. If it stays red: LapTime entry Device = Lynx, network port 5000."))
    packets = int(status.get("scoreboard_packets") or 0)
    rows.append(_row("FinishLynx", "fl_feed", "Scoreboard feed from FinishLynx (UDP 5891)",
                     True if packets else (None if not fl_up else False),
                     f"{packets} packets" if packets else "nothing heard yet",
                     "Arm an event in FinishLynx; the 'Middleware' scoreboard entry must send to 127.0.0.1:5891."))

    # ------------------------------------------------------------ folders
    rfid = ntpath.join(results, lynx_cfg.RFID_SUBDIR)
    rows.append(_row("Folders", "rfid_dir", f"Results folder + RFID subfolder ({rfid})", os.path.isdir(rfid),
                     "exists" if os.path.isdir(rfid) else "missing",
                     f"Create {rfid} (INSTALL.bat does)."))
    svc = getattr(app, "meet_service", None)
    sl = svc.start_lists_status() if svc is not None and getattr(svc, "active", False) else None
    rows.append(meet_start_lists_row(sl, rfid) if sl is not None else meetpro_row(results, rfid))
    if sl is not None and hasattr(svc, "bib_check"):
        try:
            rows.append(bib_check_row(svc.bib_check()))
        except Exception:                          # the meet closed meanwhile
            logger.debug("bib check row skipped", exc_info=True)

    # ------------------------------------------------------------ camera start
    if cfg.camera_start.enabled:
        from .camera_start import find_tshark
        tshark = find_tshark(cfg.camera_start.tshark_path)
        rows.append(_row("Camera start", "tshark", "Wireshark (tshark + Npcap) for starts from the camera",
                         bool(tshark), tshark or "not installed",
                         "Install Wireshark from wireshark.org (tick Npcap). Without it, set guns by hand."))
        cam = status.get("camera_start") or {}
        # camera_host auto: show the address learned from the replies (2026-10-11)
        host = cam.get("camera_ip") or cfg.camera_start.camera_host
        rows.append(_row("Camera start", "camera_seen", f"Hearing the camera ({host})",
                         True if cam.get("receiving") else (None if not tshark else False),
                         f"{cam.get('replies', 0)} replies" if cam.get("receiving") else (cam.get("error") or "nothing heard"),
                         "FinishLynx open with the camera connected, and this PC on the camera's network."))
    return rows


if __name__ == "__main__":
    import sys
    sys.exit(main())
