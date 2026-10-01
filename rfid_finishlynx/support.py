"""Send logs for support (2026-10-01).

The Console chip's "Send logs" zips what is needed to diagnose a problem
from afar and uploads it to the private logs repo on GitHub, as
<site>/<date>/<time>.zip, with the operator's note inside:

  NOTE.txt, VERSION.txt, status.json, setup-check.json, recent-warnings.txt,
  today's and yesterday's log, update.log, races-state.json and config.yaml
  (with the support key taken out).

The upload uses config support.logs_token: a GitHub fine-grained token
that can only write to that one repo. Logs carry athletes' names and
bibs, so they go to the private repo only - never the public one.
"""

import base64
import io
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

LOG_TAIL_BYTES = 20_000_000      # today's log in full on any normal day (it rotates daily)


def site_name(app) -> str:
    raw = (app.config.support.site_name or socket.gethostname() or "pc").strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.") or "pc"


def _tail(path: str, limit: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        return f.read()


def redact_config(text: str) -> str:
    return re.sub(r"(?m)^(\s*logs_token:).*$", r"\1 (removed)", text)


def bundle(app, note: str = "") -> bytes:
    """The zip, built from whatever is there; a missing piece is noted, not fatal."""
    from . import updater
    out = io.BytesIO()
    missing = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("NOTE.txt", f"{datetime.now():%Y-%m-%d %H:%M:%S} from {site_name(app)}\n\n{note or '(no note)'}\n")

        def add_file(name, path, tail=False, transform=None):
            try:
                if tail:
                    data = _tail(path, LOG_TAIL_BYTES)
                else:
                    with open(path, "rb") as f:
                        data = f.read()
                if transform:
                    data = transform(data.decode("utf-8", "replace")).encode("utf-8")
                z.writestr(name, data)
            except (OSError, TypeError):
                missing.append(name)

        root = updater.ROOT
        add_file("VERSION.txt", os.path.join(root, "VERSION.txt"))
        add_file("update.log", os.path.join(root, "update.log"))
        cfg = app.config
        if getattr(app, "config_path", None):
            add_file("config.yaml", app.config_path, transform=redact_config)
        if cfg.logging.file:
            add_file(os.path.basename(cfg.logging.file), cfg.logging.file, tail=True)
            # yesterday's too (main.DailyLogFile names it <log>.YYYY-MM-DD):
            # a problem reported the morning after happened there
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            prev = f"{cfg.logging.file}.{yesterday}"
            if os.path.exists(prev):
                add_file(os.path.basename(prev), prev, tail=True)
        state = getattr(cfg.races, "state_file", "") or ""
        if state:
            add_file("races-state.json", state)
        for name, fn in (("status.json", lambda: app.get_status()),
                         ("setup-check.json", lambda: __import__(
                             "rfid_finishlynx.setup_check", fromlist=["run"]).run(app))):
            try:
                z.writestr(name, json.dumps(fn(), indent=1, default=str))
            except Exception as e:
                z.writestr(name, json.dumps({"error": str(e)}))
        try:
            from .web import _log_handler
            lines = [f"{r['timestamp']} {r['level']} {r['message']}"
                     for r in _log_handler.get_logs(["WARNING", "ERROR", "CRITICAL"])]
            z.writestr("recent-warnings.txt", "\n".join(lines) + "\n")
        except Exception:
            missing.append("recent-warnings.txt")
        if missing:
            z.writestr("MISSING.txt", "\n".join(missing) + "\n")
    return out.getvalue()


class SendError(Exception):
    pass


def send(app, note: str = "") -> dict:
    """Upload the bundle; returns {path, url, bytes}."""
    sp = app.config.support
    if not sp.logs_token:
        raise SendError("no support key on this PC yet - paste the one you were given (Console chip)")
    data = bundle(app, note)
    now = datetime.now()
    path = f"{site_name(app)}/{now:%Y-%m-%d}/{now:%H%M%S}.zip"
    body = json.dumps({"message": f"Logs from {site_name(app)} {now:%Y-%m-%d %H:%M}"
                                  + (f": {note[:60]}" if note else ""),
                       "content": base64.b64encode(data).decode()}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{sp.logs_repo}/contents/{path}",
                                 data=body, method="PUT",
                                 headers={"Authorization": f"Bearer {sp.logs_token}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "rfid-finishlynx-support"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        why = {401: "the support key was refused (expired or mistyped)",
               403: "the support key may not write to the logs repo",
               404: "the support key cannot see the logs repo"}.get(e.code, f"GitHub said {e.code}")
        raise SendError(why)
    except (urllib.error.URLError, OSError) as e:
        raise SendError(f"could not reach GitHub - is this PC online? ({e})")
    logger.info(f"Logs sent for support: {path} ({len(data):,} bytes)")
    return {"path": path, "url": (resp.get("content") or {}).get("html_url", ""), "bytes": len(data)}
