"""MileSplit website session: reads the meet manager pages the v3 API doesn't
expose (2026-10-03, user's ask: the setup page's entries Close Date/time,
dates, teams).

The website doesn't take the v3 API token. Its own login (login.js on
milesplit.com) is two steps, done here with stdlib urllib:
  1. POST {email, password} to https://api30.milesplit.com/api/tokens ->
     {user_id, token, exp, refresh_token, refresh_token_exp}
  2. POST the login form to https://<state>.milesplit.com/login with
     user = base64(JSON of those tokens), which sets the session cookie.
The page loads reCAPTCHA but (2026-10-03) login.js never sends a token.

Cookies and the login live in memory only, for this process. Scraping is
fragile by nature: if MileSplit changes the pages this returns fewer fields,
it doesn't break the rest of the console.
"""

from __future__ import annotations

import base64
import html as htmllib
import http.cookiejar
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TOKENS_URL = "https://api30.milesplit.com/api/tokens"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0"
TIMEOUT_S = 30
READ_ONLY_PAGES = frozenset({"setup", "teams", "athletes", "divisions", "events"})
HOST_RE = re.compile(r"^[a-z0-9-]+\.milesplit\.com$")


class WebError(Exception):
    pass


class NotLoggedIn(WebError):
    pass


def meet_host(url: Optional[str]) -> str:
    """The state site of a meet (sc.milesplit.com from its meetUrl); only
    *.milesplit.com is accepted."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    if not HOST_RE.match(host):
        raise WebError(f"not a MileSplit meet address: {url!r}")
    return host


class _FormFields(HTMLParser):
    """Every input/select/textarea with its label, plus the page title and
    the manager's nav links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: List[dict] = []
        self.labels: Dict[str, str] = {}
        self.links: List[Tuple[str, str]] = []
        self.title = ""
        self._label_for: Optional[str] = None
        self._label_text: List[str] = []
        self._select: Optional[dict] = None
        self._option: Optional[dict] = None
        self._textarea: Optional[dict] = None
        self._a: Optional[str] = None
        self._a_text: List[str] = []
        self._in_title = False
        self._last_text = ""

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "label":
            self._label_for, self._label_text = a.get("for", ""), []
        elif tag == "input":
            t = a.get("type", "text").lower()
            if t in ("submit", "button", "image", "reset", "file"):
                return
            f = {"tag": "input", "type": t, "name": a.get("name", ""), "id": a.get("id", ""),
                 "value": a.get("value", ""), "placeholder": a.get("placeholder", "")}
            if t in ("checkbox", "radio"):
                f["checked"] = "checked" in a
            self.fields.append(f)
        elif tag == "select":
            self._select = {"tag": "select", "name": a.get("name", ""), "id": a.get("id", ""),
                            "value": "", "selected": [], "options": 0}
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value", ""), "selected": "selected" in a, "text": []}
        elif tag == "textarea":
            self._textarea = {"tag": "textarea", "name": a.get("name", ""), "id": a.get("id", ""),
                              "value": [], "type": "textarea"}
        elif tag == "a" and a.get("href"):
            self._a, self._a_text = a["href"], []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "label" and self._label_for is not None:
            if self._label_for:
                self.labels[self._label_for] = " ".join("".join(self._label_text).split())
            self._label_for = None
        elif tag == "option" and self._option is not None and self._select is not None:
            text = " ".join("".join(self._option["text"]).split())
            self._select["options"] += 1
            if self._option["selected"]:
                self._select["selected"].append(text)
                self._select["value"] = self._option["value"]
            self._option = None
        elif tag == "select" and self._select is not None:
            self._select["type"] = "select"
            self.fields.append(self._select)
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            self._textarea["value"] = "".join(self._textarea["value"]).strip()
            self.fields.append(self._textarea)
            self._textarea = None
        elif tag == "a" and self._a is not None:
            self.links.append((self._a, " ".join("".join(self._a_text).split())))
            self._a = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._label_for is not None:
            self._label_text.append(data)
        if self._option is not None:
            self._option["text"].append(data)
        if self._textarea is not None:
            self._textarea["value"].append(data)
        if self._a is not None:
            self._a_text.append(data)


def parse_form_page(html: str) -> dict:
    p = _FormFields()
    p.feed(html)
    fields = []
    for f in p.fields:
        if not f.get("name") or f.get("type") == "hidden":
            continue
        f = dict(f)
        f["label"] = p.labels.get(f.get("id") or "", "")
        if f.get("type") == "select":
            f["text"] = ", ".join(f.pop("selected"))
        fields.append(f)
    manager = [{"href": h, "text": t} for h, t in p.links if "/manager" in h and t]
    seen, links = set(), []
    for l in manager:
        if l["href"] not in seen:
            seen.add(l["href"])
            links.append(l)
    return {"title": " ".join(htmllib.unescape(p.title).split()), "fields": fields, "links": links}


def _field(fields: List[dict], name: str) -> str:
    for f in fields:
        if f.get("name") == name:
            return (f.get("text") or f.get("value") or "").strip()
    return ""


def _checked(fields: List[dict], names: List[str]) -> List[str]:
    return [f.get("label") or f["name"] for f in fields
            if f.get("name") in names and f.get("checked")]


def entries_close(fields: List[dict]) -> Optional[str]:
    """Setup form's registration Close Date + Time ("2026-09-29 9:00 PM")."""
    d, t = _field(fields, "registrationCloseDate"), _field(fields, "registrationCloseTime")
    return f"{d} {t}".strip() or None


_STAT_RE = re.compile(r'class="(teamsEntered|teamsScheduled|teamsTotal|individualEvents|athletesTotal|'
                      r'eventsEntered)"[^>]*>(.*?)</div>', re.S)


def header_stats(html: str) -> Dict[str, int]:
    """The counts in every manager page's header (Teams Entered: 40 ...)."""
    out = {}
    for key, text in _STAT_RE.findall(html):
        m = re.search(r"([\d,]+)\s*$", " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", text)).split()))
        if m:
            out[key] = int(m.group(1).replace(",", ""))
    return out


def setup_summary(html: str) -> dict:
    page = parse_form_page(html)
    f = page["fields"]
    return {
        "name": _field(f, "meetName"), "date_start": _field(f, "dateStart"), "date_end": _field(f, "dateEnd"),
        "venue": _field(f, "venue"), "host": _field(f, "hostTeam"), "timer": _field(f, "addTimer"),
        "entries_open": f"{_field(f, 'registrationOpenDate')} {_field(f, 'registrationOpenTime')}".strip(),
        "entries_close": entries_close(f), "timezone": _field(f, "timezone"),
        "levels": _checked(f, ["hs", "ms", "college", "elementary", "open"]),
        "stats": header_stats(html),
    }


_TEAM_ROW = re.compile(r'<tr data-session-id="(\d+)">(.*?)</tr>', re.S)


def teams_list(html: str) -> List[dict]:
    """The Teams page table: team, its MileSplit id, entries, status."""
    teams = []
    for session, row in _TEAM_ROW.findall(html):
        cells = [" ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", c)).split())
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        tid = re.search(r'name="invites\[\]"[^>]*value="(\d+)"', row)
        if len(cells) < 3:
            continue
        entries = re.sub(r"[^\d]", "", cells[2])
        teams.append({"name": cells[1], "milesplit_id": int(tid.group(1)) if tid else None,
                      "entries": int(entries) if entries else None,
                      "status": cells[-1] if len(cells) >= 6 else "", "session": int(session)})
    return teams


class WebSession:
    def __init__(self, opener=None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._login: Optional[Tuple[str, str]] = None
        self._hosts: set = set()
        self._lock = threading.Lock()

    def _open(self, url: str, data: Optional[bytes] = None, headers: Optional[dict] = None):
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            resp = self.opener.open(req, timeout=TIMEOUT_S)
            return resp.status, resp.geturl(), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, url, e.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as e:
            raise WebError(f"network error: {getattr(e, 'reason', e)}") from None

    def remember(self, email: str, password: str) -> None:
        """Keep the login in memory so each state site can be logged into
        when first needed."""
        with self._lock:
            self._login = (email, password)
            self._hosts.clear()

    def forget(self) -> None:
        with self._lock:
            self._login = None
            self._hosts.clear()
            self.jar.clear()

    @property
    def has_login(self) -> bool:
        return self._login is not None

    def login(self, host: str) -> None:
        if self._login is None:
            raise NotLoggedIn("log in to MileSplit in the console first")
        email, password = self._login
        status, _, body = self._open(TOKENS_URL, json.dumps({"email": email, "password": password}).encode(),
                                     {"Content-Type": "application/json", "Accept": "application/json",
                                      "Origin": f"https://{host}", "Referer": f"https://{host}/login"})
        try:
            tok = json.loads(body)
        except ValueError:
            tok = {}
        if status >= 300 or not tok.get("token"):
            raise WebError(f"MileSplit website login failed (HTTP {status}: "
                           f"{tok.get('message') or body[:120]!s})")
        user = base64.b64encode(json.dumps({
            "flo_id": tok.get("user_id"), "jwt_token": tok.get("token"), "jwt_token_expire": tok.get("exp"),
            "jwt_refresh_token": tok.get("refresh_token"),
            "jwt_refresh_token_expire": tok.get("refresh_token_exp")}).encode()).decode()
        form = urllib.parse.urlencode({"email": email, "password": password, "user": user,
                                       "next": f"https://{host}/"}).encode()
        status, final, _ = self._open(f"https://{host}/login", form,
                                      {"Content-Type": "application/x-www-form-urlencoded",
                                       "Origin": f"https://{host}", "Referer": f"https://{host}/login"})
        if status >= 400 or urllib.parse.urlparse(final).path.startswith("/login"):
            raise WebError(f"MileSplit website login was not accepted (HTTP {status}, ended at {final})")
        with self._lock:
            self._hosts.add(host)
        logger.info("MileSplit website: logged in on %s", host)

    def get(self, host: str, path: str) -> str:
        """A manager page's HTML, logging in to the state site when needed."""
        m = re.match(r"^/meets/\d+/manager/([a-z-]+)$", path)
        if not m or m.group(1) not in READ_ONLY_PAGES:
            raise WebError(f"refusing to fetch {path!r}: only read-only manager pages")
        url = f"https://{host}{path}"
        for attempt in (0, 1):
            if host not in self._hosts:
                self.login(host)
            status, final, body = self._open(url)
            if urllib.parse.urlparse(final).path.startswith("/login"):
                with self._lock:
                    self._hosts.discard(host)
                if attempt:
                    raise WebError(f"MileSplit sent {path} back to its login page (no access?)")
                continue
            if status >= 400:
                raise WebError(f"MileSplit {path}: HTTP {status}")
            return body
        raise WebError("unreachable")

    def meet_page(self, host: str, meet_id: int, page: str = "setup") -> dict:
        """One read-only manager page, parsed. Pages are allowlisted: the
        manager menu also has GET links that change things (setup?deleteAll=1
        deletes every entry), and nothing here may ever request those."""
        if page not in READ_ONLY_PAGES:
            raise WebError(f"page {page!r} is not one of {sorted(READ_ONLY_PAGES)}")
        html = self.get(host, f"/meets/{int(meet_id)}/manager/{page}")
        out = parse_form_page(html)
        out["stats"] = header_stats(html)
        if page == "setup":
            out["summary"] = setup_summary(html)
        if page == "teams":
            out["teams"] = teams_list(html)
        out["url"] = f"https://{host}/meets/{int(meet_id)}/manager/{page}"
        return out

    def meet_details(self, host: str, meet_id: int) -> dict:
        """Setup summary + team list: what the meet list's Details shows."""
        setup = self.get(host, f"/meets/{int(meet_id)}/manager/setup")
        teams = self.get(host, f"/meets/{int(meet_id)}/manager/teams")
        return dict(setup_summary(setup), teams=teams_list(teams), meet_id=int(meet_id))
