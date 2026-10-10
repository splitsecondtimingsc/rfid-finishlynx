"""Rehearse two races run together: JV Boys 7-9 and JV Boys 10-12 off one gun.

Starts its own console on a throwaway folder, imports a mock roster (6 teams,
7-9 bibs 3xx, 10-12 bibs 5xx; Bluff short in 7-9 so it can't score), runs
the two races together (one Lynx event, scored separately), fires the gun and
then brings the boys in one by one:

  * read by the mat: a real tag read through the reader's own code path
    (reader FINISH), so the race pane and the merged .lif in RFID/ fill in;
  * missed by the mat (~10%): only FinishLynx has him, as if you typed the
    bib in FinishLynx by hand.

FinishLynx's own .lif in the results folder is rewritten a few seconds behind
each finisher, like evaluating on the camera, with everyone it has so far.

    python tools/sim_run_together.py                  # console: http://127.0.0.1:8091/
    python tools/sim_run_together.py --interval 5 --fl-lag 8
    python tools/sim_run_together.py --miss 0.2       # or --miss 305,512,520
    python tools/sim_run_together.py --dir D:\\sim     # where the meet folder goes

Keys while it runs: p = pause / resume, q = stop sending (the console stays
up). Ctrl+C quits.

Safe next to a live console: it takes its own hardware lock (RFID_HW_LOCK)
and never starts the hardware - Start is disabled in this console, and the
script refreshes the merged .lif itself. Nothing outside --dir is written.
"""
import argparse
import io
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MARK = ".rfid-sim"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dir", default=os.path.join(tempfile.gettempdir(), "rfid-sim-run-together"),
                   help="throwaway folder for the meet (wiped at start)")
    p.add_argument("--port", type=int, default=8091,
                   help="first port to try; the next free one is used if it is taken")
    p.add_argument("--interval", type=float, default=3.0, help="seconds between finishers")
    p.add_argument("--fl-lag", type=float, default=4.0,
                   help="seconds FinishLynx saves behind each finisher")
    p.add_argument("--miss", default="0.10",
                   help="share of bibs the mat misses (0.10), or a list of bibs (305,512)")
    p.add_argument("--seed", type=int, default=7, help="same seed = same race")
    return p.parse_args()


# ------------------------------------------------------------------ setup

def fresh_dir(path):
    if os.path.exists(path):
        if not os.path.exists(os.path.join(path, MARK)):
            sys.exit(f"{path} exists and is not a simulation folder; pick another --dir")
        shutil.rmtree(path)
    os.makedirs(os.path.join(path, "Finish_Lynx_Results", "RFID"))
    open(os.path.join(path, MARK), "w").close()


def write_config(path):
    import yaml
    with open(os.path.join(ROOT, "config.example.yaml"), encoding="utf-8") as f:
        c = yaml.safe_load(f)
    c["mode"] = "finish_line"
    c["event_directory"] = os.path.join(path, "Finish_Lynx_Results")
    c.setdefault("races", {})["state_file"] = os.path.join(path, "races-state.json")
    c["races"]["enabled"] = True
    c.setdefault("logging", {})["file"] = os.path.join(path, "console.log")
    for k in ("led_clock", "camera_start", "scoreboard", "lora"):
        c.setdefault(k, {})["enabled"] = False
    cfg = os.path.join(path, "config.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        yaml.safe_dump(c, f, sort_keys=False)
    return cfg


def free_port(first):
    """The first port from `first` nobody listens on (2026-10-06: Docker
    held 8091 on the laptop, and the old check took its answer for ours)."""
    for port in range(first, first + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    sys.exit(f"no free port in {first}-{first + 49}")


def start_console(cfg_path, port, log_path):
    import logging
    from rfid_finishlynx import web
    from rfid_finishlynx.app import Application
    from rfid_finishlynx.config import load_config

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.FileHandler(log_path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(h)
    for n in ("werkzeug", "engineio", "socketio"):
        logging.getLogger(n).setLevel(logging.WARNING)
    web.install_log_handler()

    app = Application(load_config(cfg_path), cfg_path)
    if not app.hardware_allowed:
        sys.exit(f"console came up in preview ({app.preview_reason}): the merged .lif would not be written")

    def no_start(*_a, **_k):
        logging.getLogger("sim").warning("Simulation console: Start is disabled (no hardware)")
        return False
    app.start = no_start
    web.set_application(app)
    app.set_tag_read_callback(web.emit_tag_read)
    app.set_remote_unit_change_callback(web.emit_remote_unit_update)
    app.set_process_for_finishlynx_callback(web.process_read_for_finishlynx)
    threading.Thread(target=web.run_web_server, kwargs={"host": "127.0.0.1", "port": port},
                     daemon=True).start()
    import json
    import urllib.request
    for _ in range(100):                       # ours answers /api/meet with JSON
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meet", timeout=0.5) as r:
                if "active" in json.load(r):
                    break
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    else:
        sys.exit(f"the simulation console did not come up on port {port}")
    return app, web.app.test_client()


def call(client, method, path, **kw):
    r = getattr(client, method)(path, **kw)
    if r.status_code != 200:
        sys.exit(f"{method.upper()} {path}: {r.status_code} {r.get_data(as_text=True)[:300]}")
    return r.get_json()


def set_up_meet(client):
    from tests.test_run_together_meet import roster_csv
    call(client, "post", "/api/meet/import/file", content_type="multipart/form-data", data={
        "file": (io.BytesIO(roster_csv().encode()), "jv-rehearsal.csv"),
        "name": "JV Rehearsal (simulation)", "date": date.today().isoformat()})
    races = {r["rk"]: r for r in call(client, "get", "/api/meet/races")["races"]}
    young = next(k for k, v in races.items() if "7-9" in v["name"])
    old = next(k for k, v in races.items() if "10-12" in v["name"])
    call(client, "post", f"/api/meet/races/{young}/run-together", json={"races": [old]})
    return young


def find_pane(app, rk, wait=5.0):
    ev, rnd, heat = map(int, rk.split("-"))
    end = time.time() + wait
    while time.time() < end:
        for k, r in app.race_manager.races.items():
            if (r.event, r.round, r.heat) == (ev, rnd, heat):
                return k
        time.sleep(0.1)
    sys.exit(f"no race pane for {rk}")


def build_field(app, rk, rnd):
    """[(secs, bib, last, first, team, division)] in finish order; 10-12 a
    bit faster, the fields mixed."""
    meet = app.meet_service.meet
    race = meet.race(*map(int, rk.split("-")))
    teams = {t.id: t.name for t in meet.teams}
    field = []
    for e in race.entries:
        a = meet.athlete(e.athlete_id)
        div = meet.event(e.event).division or meet.event(e.event).name
        base = 19 * 60 if "7-9" in div else 17 * 60
        field.append((round(base + rnd.uniform(0, 300), 3), a.bib, a.last, a.first,
                      teams.get(a.team_id, ""), div))
    field.sort()
    return field


def pick_missed(field, spec, rnd):
    bibs = [f[1] for f in field]
    if "," in spec or (spec.isdigit() and len(spec) > 1 and not spec.startswith("0")):
        want = {b.strip() for b in spec.split(",") if b.strip()}
        unknown = want - set(bibs)
        if unknown:
            sys.exit(f"--miss: not in the race: {', '.join(sorted(unknown))}")
        return want
    n = round(len(bibs) * float(spec))
    return set(rnd.sample(bibs, n))


# ------------------------------------------------------------- FinishLynx

def race_time(secs):
    m, s = divmod(secs, 60)
    return f"{int(m)}:{s:06.3f}"


def fl_lif(event, name, gun, rows):
    """FinishLynx's saved event: header, then place,bib,lane,last,first,team,time,..."""
    start = gun.strftime("%H:%M:%S.%f")[:-3]
    lines = [f"{event},1,1,{name},,,,,,5000,{start}"]
    for i, (secs, bib, last, first, team, _div) in enumerate(sorted(rows), 1):
        t = race_time(secs)
        lines.append(f"{i},{bib},,{last},{first},{team},{t},,{t},,{t},{start},M,,,{t},{t},4.5,{t}")
    return "\r\n".join(lines) + "\r\n"


# ------------------------------------------------------------------- keys

class Keys:
    """p / q from the terminal without Enter (Windows and Unix)."""

    def __init__(self):
        self.old = None
        if sys.stdin.isatty() and os.name != "nt":
            import termios
            import tty
            self.old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

    def get(self):
        if os.name == "nt":
            import msvcrt
            return msvcrt.getwch().lower() if msvcrt.kbhit() else ""
        if self.old is None:
            return ""
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1).lower()
        return ""

    def close(self):
        if self.old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old)


# ------------------------------------------------------------------- main

def main():
    a = parse_args()
    d = os.path.abspath(a.dir)
    fresh_dir(d)
    os.environ.pop("RFID_PREVIEW", None)
    os.environ["RFID_HW_LOCK"] = os.path.join(d, "hw.lock")
    cfg = write_config(d)
    a.port = free_port(a.port)
    app, client = start_console(cfg, a.port, os.path.join(d, "console.log"))

    rk = set_up_meet(client)
    pane = find_pane(app, rk)
    rnd = random.Random(a.seed)
    field = build_field(app, rk, rnd)
    missed = pick_missed(field, a.miss, rnd)

    from rfid_finishlynx.rfid_reader import TagRead
    mgr = app.race_manager
    race = mgr.races[pane]
    gun = (datetime.now() - timedelta(seconds=field[-1][0] + 120)).replace(microsecond=0)
    call(client, "post", f"/api/races/{pane}/gun", json={"clock": gun.isoformat(timespec="milliseconds")})
    lynx_dir, rfid_dir = app.lif_dirs()
    fl_path = os.path.join(lynx_dir, mgr.lif_name(race))

    print(f"""
  Console:          http://127.0.0.1:{a.port}/   (Meet tab -> Races / Results)
  Race:             {' + '.join(sorted({f[5] for f in field}))}  (Lynx event {race.event}, {len(field)} boys)
  FinishLynx .lif:  {fl_path}
  Merged .lif:      {os.path.join(rfid_dir, mgr.lif_name(race))}
  Gun:              {gun:%H:%M:%S}   finisher every {a.interval:g} s, FinishLynx saves {a.fl_lag:g} s behind
  Mat misses:       {', '.join(sorted(missed, key=int)) or 'none'}  (FinishLynx only)

  Keys: p pause/resume, q stop sending; Ctrl+C quits.
""")

    keys = Keys()
    fl_rows, pending, sent = [], [], 0
    paused, stopped = False, False
    next_at, last_refresh = time.time() + 1.0, 0.0
    try:
        while True:
            k = keys.get()
            if k == "p":
                paused = not paused
                print("  -- paused --" if paused else "  -- resumed --")
                if not paused:
                    next_at = time.time() + a.interval
            elif k == "q" and not stopped:
                stopped = True
                print("  -- stopped sending; the console stays up (Ctrl+C to quit) --")
            now = time.time()
            if not (paused or stopped) and sent < len(field) and now >= next_at:
                f = field[sent]
                secs, bib, last, first, team, div = f
                sent += 1
                next_at = now + a.interval
                how = "mat missed -> FinishLynx only" if bib in missed else "mat read"
                if bib not in missed:
                    app._on_tag_read(TagRead(bib=bib, timestamp=gun + timedelta(seconds=secs), epc="",
                                             rssi=-55, antenna=1, reader_id="FINISH"))
                # FinishLynx's time is the torso on the film: a touch ahead of the chip
                film = secs - (rnd.uniform(0.05, 0.25) if bib not in missed else 0)
                pending.append((now + a.fl_lag, (round(film, 3),) + f[1:]))
                print(f"  {sent:3d}  #{bib:>4}  {(first + ' ' + last)[:20]:<20} {team[:18]:<18} {div:<14} "
                      f"{race_time(secs)[:-1]}  {how}")
            due = [r for t, r in pending if t <= now]
            if due:
                pending = [(t, r) for t, r in pending if t > now]
                fl_rows += due
                with open(fl_path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(fl_lif(race.event, race.name, gun, fl_rows))
                print(f"       FinishLynx saved {mgr.lif_name(race)}: {len(fl_rows)} timed")
                last_refresh = 0.0
            if now - last_refresh >= 2.0:          # what the hardware supervisor does
                last_refresh = now
                app.learn_film_offsets()
                app.refresh_merged_lifs()
                if sent == len(field) and not pending and not stopped:
                    stopped = True
                    print(f"\n  All {len(field)} in ({len(missed)} FinishLynx only). Check Meet -> Results;"
                          " Mark final splits the two races. Ctrl+C to quit.")
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n  bye")
    finally:
        keys.close()
        os._exit(0)


if __name__ == "__main__":
    main()
