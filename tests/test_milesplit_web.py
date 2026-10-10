import base64
import io
import json
import unittest
import urllib.parse

from rfid_finishlynx.meet import milesplit_web as w

SETUP = """<html><head><title>Test Meet Meet Manager</title></head><body>
<div class="teamsEntered">Teams Entered: 12</div><div class="teamsScheduled">Teams Scheduled: 1</div>
<div class="teamsTotal">Teams Total:
   13 </div><div class="athletesTotal">Athletes Total: 1,234</div>
<nav><a href="/meets/9/manager/setup">Setup</a><a href="/meets/9/manager/setup?deleteAll=1">Delete All Entries</a></nav>
<form>
<label for="meetName">Meet Name</label><input type="text" id="meetName" name="meetName" value="Test Meet">
<label for="dateStart">Start Date</label><input type="date" id="dateStart" name="dateStart" value="2026-10-03">
<label for="dateEnd">End Date</label><input type="date" id="dateEnd" name="dateEnd" value="2026-10-03">
<label for="registrationCloseDate">Close Date</label><input type="date" id="registrationCloseDate" name="registrationCloseDate" value="2026-09-29">
<label for="registrationCloseTime">Time</label><select id="registrationCloseTime" name="registrationCloseTime"><option value="20">8:00 PM</option><option value="21" selected>9:00 PM</option></select>
<label for="hs">High School</label><input type="checkbox" id="hs" name="hs" value="1" checked>
<label for="ms">Middle School</label><input type="checkbox" id="ms" name="ms" value="1">
<input type="radio" name="entryVisibility" value="afterClose"><input type="hidden" name="secret" value="x">
</form></body></html>"""

TEAMS = """<table><tbody>
<tr data-session-id="501"><td><input name="invites[]" type="checkbox" value="2308"></td>
<td><a href="/meets/9/manager/sessions/501">Team A</a></td><td><a href="#">32</a></td>
<td class="total">$1</td><td class="balance">$1</td><td>Registered 5/18</td></tr>
<tr data-session-id="502"><td><input name="invites[]" type="checkbox" value="77"></td>
<td><a href="#">Team &amp; B</a></td><td><a href="#">4</a></td><td>$0</td><td>$0</td><td>Registered 6/1</td></tr>
</tbody></table>"""


class Resp:
    def __init__(self, status, url, body):
        self.status, self._url, self._body = status, url, body

    def geturl(self):
        return self._url

    def read(self):
        return self._body.encode()


class FakeOpener:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def open(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, req.data))
        return self.pages(req)


class TestParsers(unittest.TestCase):
    def test_setup_summary(self):
        s = w.setup_summary(SETUP)
        self.assertEqual(s["entries_close"], "2026-09-29 9:00 PM")
        self.assertEqual((s["date_start"], s["name"]), ("2026-10-03", "Test Meet"))
        self.assertEqual(s["levels"], ["High School"])
        self.assertEqual(s["stats"], {"teamsEntered": 12, "teamsScheduled": 1, "teamsTotal": 13,
                                      "athletesTotal": 1234})

    def test_hidden_fields_dropped(self):
        names = [f["name"] for f in w.parse_form_page(SETUP)["fields"]]
        self.assertNotIn("secret", names)

    def test_teams_list(self):
        t = w.teams_list(TEAMS)
        self.assertEqual(t[0], {"name": "Team A", "milesplit_id": 2308, "entries": 32,
                                "status": "Registered 5/18", "session": 501})
        self.assertEqual(t[1]["name"], "Team & B")

    def test_meet_host(self):
        self.assertEqual(w.meet_host("https://sc.milesplit.com/meets/9-x"), "sc.milesplit.com")
        for bad in ("https://evil.com/meets/9", "https://milesplit.com.evil.com/", "", None):
            with self.assertRaises(w.WebError):
                w.meet_host(bad)


class TestSession(unittest.TestCase):
    def _session(self, setup_after_login=True):
        state = {"in": False}

        def pages(req):
            u = urllib.parse.urlparse(req.full_url)
            if u.netloc == "api30.milesplit.com":
                body = json.loads(req.data)
                self.assertEqual(body, {"email": "e@x", "password": "pw"})
                return Resp(201, req.full_url, json.dumps({"user_id": 1, "token": "jwt", "exp": 1}))
            if u.path == "/login":
                form = urllib.parse.parse_qs(req.data.decode())
                self.assertEqual(json.loads(base64.b64decode(form["user"][0]))["jwt_token"], "jwt")
                state["in"] = True
                return Resp(200, "https://sc.milesplit.com/", "home")
            if not state["in"]:
                return Resp(200, "https://sc.milesplit.com/login?next=x", "login")
            return Resp(200, req.full_url, SETUP if "setup" in u.path else TEAMS)
        opener = FakeOpener(pages)
        return w.WebSession(opener), opener

    def test_needs_login(self):
        s, _ = self._session()
        with self.assertRaises(w.NotLoggedIn):
            s.meet_page("sc.milesplit.com", 9)

    def test_login_then_details(self):
        s, op = self._session()
        s.remember("e@x", "pw")
        d = s.meet_details("sc.milesplit.com", 9)
        self.assertEqual(d["entries_close"], "2026-09-29 9:00 PM")
        self.assertEqual(len(d["teams"]), 2)
        logins = [c for c in op.calls if c[1].endswith("/login")]
        self.assertEqual(len(logins), 1)  # second page reuses the session

    def test_only_read_only_pages(self):
        s, op = self._session()
        s.remember("e@x", "pw")
        for page in ("setup?deleteAll=1", "mass-email", "payments", "import"):
            with self.assertRaises(w.WebError):
                s.meet_page("sc.milesplit.com", 9, page)
        with self.assertRaises(w.WebError):
            s.get("sc.milesplit.com", "/meets/9/manager/setup?deleteAll=1")
        self.assertEqual(op.calls, [])

    def test_forget(self):
        s, _ = self._session()
        s.remember("e@x", "pw")
        s.forget()
        self.assertFalse(s.has_login)


if __name__ == "__main__":
    unittest.main()
