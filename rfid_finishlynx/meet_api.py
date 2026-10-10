"""REST routes for meet management (docs/MEET-API.md), a Flask Blueprint
registered by web.py. Thin on purpose: every rule lives in MeetService
(meet_service.py) or the meet package; this file parses requests and
shapes answers.

`/api/meet/close` stays in web.py (it predates meet management and closes
the race panes); it calls MeetService.archive_to + detach.

Route arguments are named rk / aid / tid / bib, never `key`: web's
operator-action log treats a `key` view arg as a RaceManager pane key.
"""

from __future__ import annotations

import html
import logging
import os
import re
from typing import List, Optional, Tuple

from flask import Blueprint, Response, jsonify, request

from .meet_service import MeetError, parse_rk, rk_of

logger = logging.getLogger(__name__)

bp = Blueprint("meet", __name__)


def _svc():
    from . import web
    app = web._application
    svc = getattr(app, "meet_service", None) if app is not None else None
    if svc is None:
        raise MeetError("meet management is not available", 503)
    return svc


def _body() -> dict:
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _ok(**kw):
    return jsonify({"ok": True, **kw})


@bp.errorhandler(MeetError)
def _meet_error(e: MeetError):
    return jsonify(e.payload()), e.status


@bp.errorhandler(ValueError)
def _value_error(e: ValueError):
    # The meet package reports operator mistakes as ValueError with a
    # readable message (bad range row, final race, unknown sort key).
    return jsonify({"error": str(e)}), 400


# --------------------------------------------------------------- meet

@bp.route("/api/meet", methods=["GET"])
def meet_summary():
    from . import web
    app = web._application
    svc = getattr(app, "meet_service", None) if app is not None else None
    return jsonify(svc.summary() if svc is not None else {"active": False})


@bp.route("/api/meet/list", methods=["GET"])
def meet_list():
    return jsonify({"meets": _svc().list_meets()})


@bp.route("/api/meet/new", methods=["POST"])
def meet_new():
    return _ok(meet=_svc().new_meet(_body()))


def _file_only() -> bool:
    """Open a closed meet with FinishLynx (default: files back into the
    results folder, panes and reads) or {"mode": "file"}: its meet file
    only (web.reopen_closed_meet, 2026-10-05)."""
    mode = str(_body().get("mode") or "finishlynx")
    if mode not in ("finishlynx", "file"):
        raise MeetError(f"mode must be finishlynx or file, not {mode!r}", 400)
    return mode == "file"


def _closed_folder(svc, d: dict):
    """The closed-meet folder an Open names ({id} or {path}), or None."""
    from . import web
    if d.get("path"):
        return web.find_meet_folder(str(d["path"]))
    meet_id = str(d.get("id") or "")
    if re.fullmatch(r"[A-Za-z0-9._-]+", meet_id):
        return svc.archived_folder(meet_id)
    return None


@bp.route("/api/meet/open/plan", methods=["GET"])
def meet_open_plan():
    """For the Open dialog (2026-10-05): is this a closed meet, and can it
    be opened with FinishLynx right now (what stops it), or only as its
    meet file. Query: id=<meet id> or path=<file in a meet folder>."""
    from . import web
    svc = _svc()
    folder = _closed_folder(svc, request.args)
    if not folder:
        return jsonify({"closed": False})
    if not os.path.isdir(folder):
        return jsonify({"closed": True, "folder": folder, "folder_missing": True})
    return jsonify({"closed": True, **web.reopen_plan(folder)})


@bp.route("/api/meet/open", methods=["POST"])
def meet_open():
    # 2026-10-05: a closed meet comes back from its meet folder (files,
    # panes, reads); one whose folder is gone opens from this console's copy.
    from . import web
    svc = _svc()
    meet_id = str(_body().get("id") or "")
    file_only = _file_only()
    folder = svc.archived_folder(meet_id) if re.fullmatch(r"[A-Za-z0-9._-]+", meet_id) else None
    if folder and os.path.isdir(folder):
        r = web.reopen_closed_meet(folder, file_only=file_only)
        return _ok(meet=r.pop("meet"), reopened=r)
    if folder and not _body().get("console_copy"):
        # Moved or deleted: opening the console's copy would bring back no
        # reads, so say where it was and let the operator choose.
        return jsonify({"error": f"This meet was closed to {folder}, which is not there now. If you moved "
                        "it, use Browse... and pick its MEET FILE in the new place.",
                        "folder_missing": folder}), 409
    # the console's copy of a closed meet has no files or reads to go with
    # it: open it as its meet file only
    return _ok(meet=svc.open_meet(meet_id, file_only=bool(folder)))


@bp.route("/api/meet/open-file", methods=["POST"])
def meet_open_file():
    from . import web
    path = _body().get("path")
    folder = web.find_meet_folder(path)
    if folder:
        r = web.reopen_closed_meet(folder, file_only=_file_only())
        return _ok(meet=r.pop("meet"), reopened=r)
    return _ok(meet=_svc().open_path(path))


# --------------------------------------------------------------- sync
# Meets shared between machines (meet_sync.py, docs/MEET-SYNC-PLAN.md).

def _app():
    from . import web
    if web._application is None:
        raise MeetError("console not ready", 503)
    return web._application


def _sync_payload(app) -> dict:
    from .meet_sync import allowed_targets, machine_name
    c = app.config.meet_sync
    allowed = list(allowed_targets())
    mode = c.mode if c.enabled else "off"
    if "git" not in allowed:                       # the public edition: the folder only
        mode = {"git": "off", "both": "folder"}.get(mode, mode)
    out = {"enabled": bool(app.meet_sync), "mode": mode, "folder": c.folder,
           "remote": c.remote if "git" in allowed else "", "machine": machine_name(c.machine),
           "preview": not getattr(app, "hardware_allowed", True), "targets_allowed": allowed}
    if app.meet_sync is not None:
        out.update(app.meet_sync.status())
        out["clashes"] = app.meet_sync.clashes()
        out["separate_copies"] = app.meet_sync.separate()     # 2026-10-10
        out["bib_dup_list"] = app.meet_sync.bib_dups()
    return out


@bp.route("/api/meet/sync", methods=["GET"])
def sync_status():
    return jsonify(_sync_payload(_app()))


@bp.route("/api/meet/sync", methods=["POST"])
def sync_now():
    app = _app()
    if app.meet_sync is None:
        raise MeetError("meet sync is off", 409)
    rep = app.meet_sync.sync_now()
    return _ok(report=rep, sync=_sync_payload(app))


@bp.route("/api/meet/sync/settings", methods=["POST"])
def sync_settings():
    """{mode: off|git|folder|both, folder, remote, machine}: saved to
    config.yaml, the sync restarts with them. remote: a git remote or a
    folder (a network share on the NAS, 2026-10-08)."""
    from .meet_sync import allowed_targets, is_git_remote
    app, d = _app(), _body()
    c = app.config.meet_sync
    mode = str(d.get("mode", c.mode) or "off").lower()
    if mode not in ("off", "git", "folder", "both"):
        raise MeetError("mode must be off, git, folder or both", 400)
    if mode in ("git", "both") and "git" not in allowed_targets():
        raise MeetError("this edition shares meets through a Google Drive folder only", 400)
    folder = str(d.get("folder", c.folder) or "").strip()
    remote = str(d.get("remote", c.remote) or "").strip()
    if mode in ("folder", "both") and not os.path.isdir(folder):
        raise MeetError(f"shared folder not found: {folder or '(none)'}", 400)
    if mode in ("git", "both"):
        if not remote:
            raise MeetError("the NAS needs a git repo (e.g. myserver:me/meets.git) or a folder", 400)
        if not is_git_remote(remote) and not os.path.isdir(remote):
            raise MeetError(f"NAS folder not found: {remote}", 400)
    c.mode, c.enabled, c.folder, c.remote = mode, mode != "off", folder, remote
    if "machine" in d:
        c.machine = str(d.get("machine") or "").strip()
    app.save_config()
    if getattr(app, "hardware_allowed", True):
        app.start_meet_sync()
    return _ok(sync=_sync_payload(app))


@bp.route("/api/meet/sync/conflicts/<meet_id>/<int:index>", methods=["POST"])
def sync_resolve(meet_id, index):
    """{take: "theirs" | "mine", target: "git" | "folder"} for one recorded
    clash; target = the place it was recorded on (default the first)."""
    app = _app()
    if app.meet_sync is None:
        raise MeetError("meet sync is off", 409)
    d = _body()
    take = d.get("take")
    if take not in ("theirs", "mine"):
        raise MeetError("take must be theirs or mine", 400)
    try:
        sync = app.meet_sync.runner(d["target"]).sync if d.get("target") else app.meet_sync.sync
        done = sync.resolve(meet_id, index, take)
    except KeyError:
        raise MeetError(f"meet sync isn't using {d.get('target')}", 404) from None
    except IndexError as e:
        raise MeetError(str(e), 404) from None
    if done is False:
        raise MeetError("a race is running: try again when it is done", 409)
    if take == "theirs":
        app.meet_sync.notify_saved(meet_id)
    return _ok(sync=_sync_payload(app))


@bp.route("/api/meet/sync/separate/<meet_id>", methods=["POST"])
def sync_separate(meet_id):
    """{machine, take: "theirs" | "mine", target: "git" | "folder"} for a
    copy of the meet another PC imported separately (2026-10-10): "theirs"
    replaces this PC's copy with it, "mine" keeps ours (the other PC then
    has to take ours)."""
    app = _app()
    if app.meet_sync is None:
        raise MeetError("meet sync is off", 409)
    d = _body()
    take = d.get("take")
    if take not in ("theirs", "mine"):
        raise MeetError("take must be theirs or mine", 400)
    try:
        sync = app.meet_sync.runner(d["target"]).sync if d.get("target") else app.meet_sync.sync
        done = sync.resolve_separate(meet_id, str(d.get("machine") or ""), take)
    except KeyError:
        raise MeetError(f"meet sync isn't using {d.get('target')}", 404) from None
    except (IndexError, OSError) as e:
        raise MeetError(str(e), 404) from None
    if done is False:
        raise MeetError("a race is running: try again when it is done", 409)
    if take == "theirs":
        app.meet_sync.notify_saved(meet_id, urgent=True)
    return _ok(sync=_sync_payload(app))


@bp.route("/api/meet/detach", methods=["POST"])
def meet_detach():
    return jsonify({"ok": True, **_svc().detach()})


@bp.route("/api/meet/info", methods=["POST"])
def meet_info():
    return _ok(meet=_svc().set_info(_body()))


@bp.route("/api/meet/divisions", methods=["POST"])
def meet_divisions():
    """{"divisions": [...]}: the meet's whole division list (2026-10-06)."""
    return _ok(meet=_svc().set_divisions(_body()))


# --------------------------------------------------------------- import

def _import_milesplit(svc, d: dict):
    if d.get("fetch"):
        # already here: refused before the minute-long fetch (2026-10-10)
        svc.check_new_meet(d.get("meet_id"), str(d.get("name") or ""), str(d.get("date") or ""))
        entries, rosters = svc.fetch_milesplit(d.get("tc_id"), d.get("meet_id"),
                                               bool(d.get("full_rosters")))
    else:
        entries, rosters = d.get("entries"), d.get("rosters")
    return svc.import_milesplit(entries, rosters, name=str(d.get("name") or ""),
                                day=str(d.get("date") or ""), tc_id=d.get("tc_id"),
                                meet_id=d.get("meet_id"), host=str(d.get("host") or ""),
                                location=str(d.get("location") or ""))


@bp.route("/api/meet/import/milesplit", methods=["POST"])
def import_milesplit():
    report, summary = _import_milesplit(_svc(), _body())
    return _ok(report=report, meet=summary)


@bp.route("/api/meet/import/file", methods=["POST"])
def import_file():
    f = request.files.get("file")
    if f is None or not f.filename:
        raise MeetError("no file uploaded (multipart field 'file')", 400)
    flag = str(request.form.get("include_roster_only", "")).lower() in ("1", "true", "on", "yes")
    report, summary = _svc().import_file(f.read(), f.filename, flag,
                                         name=request.form.get("name", ""),
                                         day=request.form.get("date", ""))
    return _ok(report=report, meet=summary)


@bp.route("/api/meet/refresh", methods=["POST"])
def meet_refresh():
    svc = _svc()
    d = _body()
    if d.get("fetch"):
        report, summary = svc.refresh_fetch(str(d.get("name") or ""), str(d.get("host") or ""),
                                            str(d.get("location") or ""),
                                            full_rosters=bool(d.get("full_rosters")))
    else:
        if not svc.active:
            raise MeetError("no meet is open", 409)
        report, summary = svc.import_milesplit(d.get("entries"), d.get("rosters"))
    return _ok(report=report, meet=summary)


# --------------------------------------------------------------- MileSplit

@bp.route("/api/meet/milesplit/login", methods=["POST"])
def ms_login():
    d = _body()
    return _ok(**_svc().login(str(d.get("email") or ""), str(d.get("password") or ""),
                              remember=bool(d.get("remember"))))


@bp.route("/api/meet/milesplit/status", methods=["GET"])
def ms_status():
    return jsonify(_svc().milesplit_status())


@bp.route("/api/meet/milesplit/logout", methods=["POST"])
def ms_logout():
    return _ok(**_svc().logout(forget=bool(_body().get("forget"))))


@bp.route("/api/meet/milesplit/meets/<int:meet_id>/counts", methods=["GET"])
def ms_meet_counts(meet_id):
    return jsonify(_svc().milesplit_meet_counts(request.args.get("tc"), meet_id))


@bp.route("/api/meet/milesplit/meets/<int:meet_id>/page", methods=["GET"])
def ms_meet_page(meet_id):
    a = request.args
    return jsonify(_svc().milesplit_meet_page(meet_id, a.get("url"), a.get("page") or "setup",
                                              raw=a.get("raw") == "1"))


@bp.route("/api/meet/milesplit/meets", methods=["GET"])
def ms_meets():
    a = request.args
    return jsonify(_svc().milesplit_meets(a.get("month"), a.get("year"), a.get("tc")))


@bp.route("/api/meet/races/<rk>/milesplit/upload", methods=["POST"])
def ms_upload(rk):
    return _ok(**_svc().upload(rk))


@bp.route("/api/meet/milesplit/live", methods=["POST"])
def ms_live():
    return _ok(**_svc().set_live(bool(_body().get("on"))))


@bp.route("/api/meet/milesplit/live/start-times", methods=["POST"])
def ms_live_start_times():
    return _ok(**_svc().send_start_times())


# --------------------------------------------------------------- teams / athletes

@bp.route("/api/meet/teams", methods=["GET"])
def teams():
    return jsonify({"teams": _svc().teams()})


@bp.route("/api/meet/teams", methods=["POST"])
def team_add():
    tid, summary = _svc().add_team(_body())
    return _ok(team_id=tid, meet=summary)


@bp.route("/api/meet/teams/<tid>", methods=["POST"])
def team_update(tid):
    return _ok(meet=_svc().update_team(tid, _body()))


@bp.route("/api/meet/athletes", methods=["GET"])
def athletes():
    a = request.args
    limit = a.get("limit")
    try:
        limit = int(limit) if limit not in (None, "") else None
        offset = int(a.get("offset") or 0)
    except ValueError:
        raise MeetError("limit and offset must be whole numbers", 400) from None
    roster = a.get("roster")
    return jsonify(_svc().athletes(q=a.get("q", ""), team=a.get("team", ""),
                                   gender=a.get("gender", ""), race=a.get("race", ""),
                                   roster=roster if roster in ("0", "1") else None,
                                   limit=limit, offset=offset, source=a.get("source", ""),
                                   sort=a.get("sort", ""),
                                   desc=a.get("desc", "") in ("1", "true"),
                                   nobib_first=a.get("nobib", "") in ("1", "true")))


@bp.route("/api/meet/athletes/search", methods=["GET"])
def athletes_search():
    a = request.args
    return jsonify({"matches": _svc().search(a.get("first", ""), a.get("last", ""),
                                             a.get("team"))})


@bp.route("/api/meet/athletes", methods=["POST"])
def athlete_add():
    view, match, summary = _svc().add_athlete(_body())
    return _ok(athlete=view, id_match=match, meet=summary)


@bp.route("/api/meet/athletes/<aid>", methods=["POST"])
def athlete_update(aid):
    view, summary = _svc().update_athlete(aid, _body())
    return _ok(athlete=view, meet=summary)


@bp.route("/api/meet/athletes/<aid>/entry", methods=["POST"])
def athlete_entry(aid):
    view, summary = _svc().set_entry(aid, _body())
    return _ok(athlete=view, meet=summary)


@bp.route("/api/meet/athletes/<aid>", methods=["DELETE"])
def athlete_delete(aid):
    return _ok(meet=_svc().delete_athlete(aid))


@bp.route("/api/meet/undo", methods=["POST"])
def undo():
    return _ok(meet=_svc().undo())


# --------------------------------------------------------------- bibs

@bp.route("/api/meet/athletes/<aid>/bib", methods=["POST"])
def bib_set(aid):
    result, summary = _svc().set_bib(aid, _body().get("bib", ""))
    return _ok(result=result, meet=summary)


@bp.route("/api/meet/bibs/swap", methods=["POST"])
def bib_swap():
    d = _body()
    result, summary = _svc().swap_bibs(d.get("a"), d.get("b"))
    return _ok(result=result, meet=summary)


@bp.route("/api/meet/bibs/next", methods=["GET"])
def bib_next():
    return jsonify({"bib": _svc().next_free_bib()})


@bp.route("/api/meet/bibs/<bib>/context", methods=["GET"])
def bib_context(bib):
    return jsonify(_svc().bib_context(bib))


@bp.route("/api/meet/bibs/<bib>/wearer", methods=["POST"])
def bib_wearer(bib):
    report, match, summary = _svc().wearer(bib, _body())
    return _ok(report=report, id_match=match, meet=summary)


@bp.route("/api/meet/bibs/assign", methods=["POST"])
def bib_assign():
    report, summary = _svc().assign(_body())
    out = {"report": report}
    if summary is not None:
        out["meet"] = summary
    return _ok(**out)


@bp.route("/api/meet/bibs/clear", methods=["POST"])
def bib_clear():
    report, summary = _svc().clear_bibs(_body())
    return _ok(report=report, meet=summary)


@bp.route("/api/meet/bibs/conflicts", methods=["GET"])
def bib_conflicts():
    return jsonify(_svc().conflicts())


@bp.route("/api/meet/bibs/check", methods=["GET"])
def bib_check():
    """The bib check (2026-10-10): bibs handed out vs the meet's bibs vs
    FinishLynx's lynx.ppl (meet/bibcheck.py)."""
    return jsonify(_svc().bib_check())


@bp.route("/api/meet/bibs/check", methods=["POST"])
def bib_check_fix():
    """{action: restore | accept | write, athlete_ids?, all?}: fix what the
    bib check found (MeetService.fix_bibs)."""
    report, summary = _svc().fix_bibs(_body())
    return _ok(report=report, check=_svc().bib_check(), meet=summary)


# --------------------------------------------------------------- id review

@bp.route("/api/meet/idreview", methods=["GET"])
def idreview():
    return jsonify(_svc().idreview())


@bp.route("/api/meet/idreview/<aid>/approve", methods=["POST"])
def idreview_approve(aid):
    return _ok(meet=_svc().approve(aid, _body().get("candidate")))


@bp.route("/api/meet/idreview/<aid>/decline", methods=["POST"])
def idreview_decline(aid):
    return _ok(meet=_svc().decline(aid))


# --------------------------------------------------------------- races

@bp.route("/api/meet/races", methods=["GET"])
def races():
    return jsonify({"races": _svc().races()})


@bp.route("/api/meet/races", methods=["POST"])
def race_add():
    rk, summary = _svc().add_race(_body())
    return _ok(race=rk, meet=summary)


@bp.route("/api/meet/races/divisions", methods=["POST"])
def race_divisions_all():
    """{"divisions": [ids], "races": [rk, ...] (omitted = every race)}:
    the same divisions for several races (2026-10-06)."""
    d = _body()
    rks = d.get("races")
    if rks is not None and (not isinstance(rks, list) or not all(isinstance(x, str) for x in rks)):
        raise MeetError("races must be a list of race keys", 400)
    return _ok(meet=_svc().set_race_divisions(d.get("divisions"), rks))


@bp.route("/api/meet/races/start-times", methods=["POST"])
def race_start_times():
    n, summary = _svc().fill_start_times(_body())
    return _ok(filled=n, meet=summary)


@bp.route("/api/meet/races/<rk>", methods=["DELETE"])
def race_cancel(rk):
    return _ok(meet=_svc().cancel_race(rk))


@bp.route("/api/meet/races/order", methods=["POST"])
def race_order():
    order = _body().get("order")
    if not isinstance(order, list):
        raise MeetError("order must be a list of race keys", 400)
    return _ok(meet=_svc().race_order(order))


@bp.route("/api/meet/races/<rk>", methods=["POST"])
def race_update(rk):
    return _ok(meet=_svc().update_race(rk, _body()))


@bp.route("/api/meet/races/<rk>/switch-in", methods=["POST"])
def race_switch_in(rk):
    aids = _body().get("athletes")
    if not isinstance(aids, list) or not all(isinstance(a, str) for a in aids):
        raise MeetError("athletes must be a list of athlete ids", 400)
    out = _svc().switch_entries(rk, aids)
    return _ok(meet=out.pop("meet"), **out)


@bp.route("/api/meet/races/<rk>/clear-entries", methods=["POST"])
def race_clear_entries(rk):
    return _ok(meet=_svc().clear_entries(rk))


@bp.route("/api/meet/races/<rk>/split-genders", methods=["POST"])
def race_split(rk):
    return _ok(meet=_svc().split_genders(rk, bool(_body().get("on"))))


@bp.route("/api/meet/races/<rk>/run-together", methods=["POST"])
def race_run_together(rk):
    others = _body().get("races")
    if not isinstance(others, list) or not all(isinstance(o, str) for o in others):
        raise MeetError("races must be a list of race keys", 400)
    rep, summary = _svc().run_together(rk, others)
    return _ok(meet=summary, report=rep)


@bp.route("/api/meet/races/<rk>/run-separately", methods=["POST"])
def race_run_separately(rk):
    rep, summary = _svc().run_separately(rk)
    return _ok(meet=summary, report=rep)


@bp.route("/api/meet/races/<rk>/results", methods=["GET"])
def race_results(rk):
    return jsonify(_svc().race_results(rk))


@bp.route("/api/meet/races/<rk>/final", methods=["POST"])
def race_final(rk):
    summary, warnings = _svc().mark_final(rk)
    return _ok(meet=summary, warnings=warnings)


@bp.route("/api/meet/races/<rk>/refinalize", methods=["POST"])
def race_refinalize(rk):
    """Reopen + Mark final in one step after a bib change (2026-10-05)."""
    summary, warnings, sent = _svc().refinalize(rk)
    return _ok(meet=summary, warnings=warnings, milesplit_sent=sent)


@bp.route("/api/meet/races/<rk>/import-lif", methods=["POST"])
def race_import_lif(rk):
    """"Select .lif file" (2026-10-09): the picked .lif is the truth for the
    race; a final race is re-finalized from it."""
    from .web import _uploaded_lif_text
    text, name = _uploaded_lif_text()
    out = _svc().import_lif(rk, text, name)
    if "meet" not in out:
        out["meet"] = _svc().summary()
    return _ok(**out)


@bp.route("/api/meet/races/<rk>/reopen", methods=["POST"])
def race_reopen(rk):
    return _ok(meet=_svc().reopen(rk))


@bp.route("/api/meet/races/<rk>/issues/<bib>", methods=["POST"])
def race_issue(rk, bib):
    """A flagged runner (2026-10-06): {"action": "ignore" | "unignore" |
    "override" | "swap" | "give", "athlete_id"?}. A final race is
    re-finalized by itself."""
    report, summary = _svc().resolve_issue(rk, bib, _body())
    return _ok(meet=summary, report=report)


@bp.route("/api/meet/races/<rk>/marks", methods=["POST"])
def race_marks(rk):
    d = _body()
    return _ok(meet=_svc().set_mark(rk, d.get("bib"), d.get("mark")))


# --------------------------------------------------------------- printing

# report -> (query params it takes, race required, pdf only). A param the
# report doesn't take is a 400 (MEET-API.md: unknown combination -> 400),
# so a sheet can't silently print unfiltered when a filter was asked for.
_FILTERS = {"places", "grades", "teams", "hide", "events"}
# (2026-10-06) `divisions` = all (default) | none | d1,d2 (those divisions
# only): the division sections of the results and award reports.
_DIVS = {"divisions"}
REPORTS = {
    "results": ({"race", "columns"} | _FILTERS | _DIVS, True, False),
    # (2026-10-03) `races=rk,rk` picks some races for the meet-wide
    # reports (user: print results for selected races, not only the meet).
    "meet-results": ({"columns", "races"} | _FILTERS | _DIVS, False, False),
    "team-scores": ({"race", "races", "events", "breakdown"} | _DIVS, False, False),
    "awards": ({"race", "races", "events"} | _DIVS, False, False),
    "award-labels": ({"race", "races", "events", "skip"} | _DIVS, False, True),
    "finish-line": ({"race", "races"}, False, True),
    "rosters": ({"teams"}, False, False),
    "team-labels": ({"teams", "skip"}, False, True),
    "competitor-labels": ({"teams", "skip"}, False, True),
    "split-scores": ({"race", "breakdown"}, True, False),
    # (2026-10-03) start-list without race = every race in run order, one
    # file ("Print all start lists" is one download); schedule = run order
    # with scheduled start times.
    "start-list": ({"race"}, False, False),
    "schedule": (set(), False, False),
    "team-bibs": ({"teams"}, False, False),
    # (2026-10-03) `team_count` is a count ("all" = every team), named apart
    # from the `teams=t1,t2` filter the other reports take.
    "announcer": ({"race", "races", "top", "team_count", "events"} | _DIVS, False, False),
}


# Printouts that go out with the bibs: printing one records its runners'
# bibs as handed out, for the bib check (2026-10-10).
HANDOUTS = ("rosters", "team-bibs", "competitor-labels")


def _csv(v: str) -> List[str]:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def _places(v: str) -> Tuple[int, Optional[int]]:
    """'1-25' -> (1, 25); '10-' -> (10, None); '25' -> top 25."""
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d*)\s*)?", v or "")
    if not m:
        raise MeetError(f"places {v!r}: use 1-25, 10- or 25", 400)
    if m.group(2) is None and "-" not in v:
        return 1, int(m.group(1))
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def _int_arg(args, name: str, default: int, lo: int, hi: int) -> int:
    v = args.get(name)
    if v in (None, ""):
        return default
    if not str(v).isdigit() or not lo <= int(v) <= hi:
        raise MeetError(f"{name} must be {lo}-{hi}", 400)
    return int(v)


def render_report(svc, name: str, fmt: str, args) -> Tuple[bytes, str]:
    """(file bytes, mimetype) for one printout of the active meet, built
    from a snapshot so a long PDF never holds the meet lock."""
    from .meet import pdf_reports, printing, reports
    spec = REPORTS.get(name)
    if spec is None or fmt not in ("pdf", "txt"):
        raise MeetError(f"no report {name}.{fmt}", 400)
    allowed, needs_race, pdf_only = spec
    if pdf_only and fmt != "pdf":
        raise MeetError(f"{name} is PDF only", 400)
    extra = sorted(k for k in args if k not in allowed)
    if extra:
        raise MeetError(f"{name} doesn't take {', '.join(extra)}", 400)
    meet = svc.snapshot()
    race = None
    if args.get("race"):
        key = parse_rk(args["race"])
        race = meet.race(*key)
        if race is None:
            raise MeetError(f"no race {rk_of(key)} in this meet", 404)
    elif needs_race:
        raise MeetError(f"{name} needs race=<event>-<round>-<heat>", 400)
    races = [race] if race is not None else None
    if args.get("races"):
        if race is not None:
            raise MeetError("give race or races, not both", 400)
        keys = [parse_rk(x) for x in _csv(args["races"])]
        missing = [rk_of(k) for k in keys if meet.race(*k) is None]
        if missing:
            raise MeetError(f"no race {', '.join(missing)} in this meet", 404)
        races = [r for r in meet.races_in_order() if r.key in set(keys)]

    filters = {}
    if args.get("places"):
        filters["places"] = _places(args["places"])
    if args.get("grades"):
        filters["grades"] = _csv(args["grades"])
    team_ids = None
    if "teams" in args:
        team_ids = [None if t.lower() in ("none", "unattached") else t for t in _csv(args["teams"])]
        filters["team_ids"] = team_ids
    if args.get("hide"):
        filters["hide"] = [h.upper() for h in _csv(args["hide"])]
    events = None
    if args.get("events"):
        try:
            events = [int(e) for e in _csv(args["events"])]
        except ValueError:
            raise MeetError("events must be event numbers, e.g. 3,4", 400) from None
        filters["events"] = events
    divisions = args.get("divisions") or None
    if divisions is not None:
        from .meet import divisions as divs_mod
        divs_mod.check_pick(meet, divisions)       # ValueError -> 400
        if name in ("results", "meet-results"):
            filters["divisions"] = divisions
    columns = _int_arg(args, "columns", 1, 1, 2)
    if columns != 1 and fmt != "pdf":
        raise MeetError("columns=2 is PDF only", 400)
    breakdown = str(args.get("breakdown", "")).lower() in ("1", "true", "yes", "on")
    skip = _int_arg(args, "skip", 0, 0, 29)
    pdf = fmt == "pdf"

    if name == "results":
        # A race nobody has marked final prints its current finishers,
        # titled NOT MARKED COMPLETE (2026-10-05, user).
        live = svc.live_results_for(rk_of(race.key)) if race.status != "final" else None
        out = pdf_reports.results_pdf(meet, race, columns=columns, live=live, **filters) if pdf \
            else printing.results_text(meet, race, live=live, **filters)
    elif name == "meet-results":
        # races not marked final print their current finishers (2026-10-06)
        live = {}
        for r in races if races is not None else meet.races_in_order():
            if r.status != "final":
                cur = svc.live_results_for(rk_of(r.key))
                if cur:
                    live[r.key] = cur
        out = pdf_reports.meet_results_pdf(meet, columns=columns, races=races, live=live, **filters) \
            if pdf else printing.meet_results_text(meet, races=races, live=live, **filters)
    elif name == "team-scores":
        out = pdf_reports.team_scores_pdf(meet, races=races, events=events, breakdown=breakdown,
                                          divisions=divisions) \
            if pdf else reports.team_scores_text(meet, races=races, events=events,
                                                 breakdown=breakdown, divisions=divisions)
    elif name == "awards":
        out = pdf_reports.awards_pdf(meet, races=races, events=events, divisions=divisions) \
            if pdf else reports.awards_text(meet, races=races, events=events, divisions=divisions)
    elif name == "announcer":
        tc = str(args.get("team_count", "") or "").strip().lower()
        kw = {"races": races, "events": events, "divisions": divisions,
              "top": _int_arg(args, "top", reports.DEFAULT_AWARDS, 1, 999)}
        if tc:
            kw["teams"] = "all" if tc == "all" else _int_arg(args, "team_count", 3, 0, 999)
        out = pdf_reports.announcer_pdf(meet, **kw) if pdf else reports.announcer_text(meet, **kw)
    elif name == "award-labels":
        labels = reports.award_labels(reports.award_winners(meet, races, events=events,
                                                            divisions=divisions))
        out = pdf_reports.labels_pdf(meet, reports.label_sheets(labels, skip=skip), "Award Labels")
    elif name == "team-labels":
        labels = reports.team_labels(meet, team_ids)
        out = pdf_reports.labels_pdf(meet, reports.label_sheets(labels, skip=skip), "Team Labels")
    elif name == "competitor-labels":
        labels = reports.competitor_labels(meet, team_ids)
        out = pdf_reports.labels_pdf(meet, reports.label_sheets(labels, skip=skip),
                                     "Competitor Labels")
    elif name == "finish-line":
        out = pdf_reports.finish_line_pdf(meet, races=races)
    elif name == "rosters":
        out = pdf_reports.rosters_pdf(meet, team_ids) if pdf else printing.rosters_text(meet, team_ids)
    elif name == "split-scores":
        results = None
        if race.status != "final":
            results = svc.live_results_for(rk_of(race.key))
            if results is None:
                raise MeetError("no results for this race yet", 400)
        out = pdf_reports.split_scores_pdf(meet, race, results=results, breakdown=breakdown) \
            if pdf else reports.split_scores_text(meet, race, results=results, breakdown=breakdown)
    elif name == "start-list":
        text = printing.start_list_text(meet, race) if race is not None \
            else printing.start_lists_text(meet)
        out = printing.to_pdf(text, title="Start List" if race is not None else "Start Lists") \
            if pdf else text
    elif name == "schedule":
        out = pdf_reports.schedule_pdf(meet) if pdf else printing.schedule_text(meet)
    else:  # team-bibs
        text = printing.team_bibs_text(meet, team_ids)
        out = printing.to_pdf(text, title="Team Bibs") if pdf else text

    if pdf:
        return out, "application/pdf"
    return out.encode("utf-8"), "text/plain; charset=utf-8"


@bp.route("/api/meet/print/<path:filename>", methods=["GET"])
def print_report(filename):
    """A printout as a download, or with inline=1 shown in the browser
    (2026-10-06, user: a generated results page should open in the browser,
    where it can be printed or saved under any name and folder; how the
    browser treats a download is its own setting, so the console opens the
    printout in a new tab instead). name=<file name> suggests the name the
    browser's Save offers. Both are taken off before render_report, whose
    unknown-parameter check stays strict."""
    name, _, fmt = filename.rpartition(".")
    inline = request.args.get("inline") in ("1", "true", "yes")
    args = {k: v for k, v in request.args.items() if k not in ("inline", "name")}
    svc = _svc()
    try:
        data, mimetype = render_report(svc, name, fmt.lower(), args)
    except (MeetError, ValueError) as e:
        if not inline:
            raise
        status = e.status if isinstance(e, MeetError) else 400
        page = ("<!doctype html><meta charset='utf-8'><title>Printout not made</title>"
                "<body style='font:15px system-ui,sans-serif;margin:40px;color:#222'>"
                f"<h2>Printout not made</h2><p>{html.escape(str(e))}</p>"
                "<p style='color:#666'>Close this tab and change the print options on the console.</p>")
        return Response(page, status=status, mimetype="text/html")
    if name in HANDOUTS:
        # what goes out with the bibs: the runners on it were handed them (2026-10-10)
        try:
            teams = [t for t in _csv(args.get("teams", "")) if t.lower() not in ("none", "unattached")]
            svc.record_handed_out(teams or None)
        except Exception:
            logger.exception("Bib check: printout not recorded as handed out")
    meet_id = svc.summary().get("id", "meet")
    race = args.get("race")
    fname =f"{meet_id}-{name}{'-' + rk_of(parse_rk(race)) if race else ''}.{fmt.lower()}"
    hint = re.sub(r"[^A-Za-z0-9._ -]+", "_", os.path.basename(request.args.get("name") or "")).strip(" .")
    if hint:
        fname = hint if hint.lower().endswith("." + fmt.lower()) else f"{hint}.{fmt.lower()}"
    disp = "inline" if inline else "attachment"
    return Response(data, mimetype=mimetype,
                    headers={"Content-Disposition": f'{disp}; filename="{fname}"'})
