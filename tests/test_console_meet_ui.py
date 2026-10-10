"""2026-10-03: the console's Meet tab (meet management UI) stays wired to
the integration contract.

- /console renders with the new chrome: the Races | Meet tab strip, the
  Meet view, the results drawer and the bib popover element;
- every /api/meet/... path the console's script calls is one that
  docs/MEET-API.md names (placeholders like ${rk} or <aid> compare equal),
  and every printed report it asks for is in the contract's report list;
- meet-wide actions sit on the Meet tab's bar; the Races toolbar keeps the
  race actions.

Hardware-free: python -m unittest tests.test_console_meet_ui
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from rfid_finishlynx import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(ROOT, 'docs', 'MEET-API.md')

# Reports the console may ask for before MEET-API.md lists them (none now:
# `announcer`, added 2026-10-03, is in the contract).
PENDING_REPORTS = set()


def _norm(path):
    """/api/meet/races/${rk}/final and /api/meet/races/<rk>/final -> .../<x>/final"""
    path = path.split('?')[0]
    path = re.sub(r'\$\{[^}]*\}', '<x>', path)
    path = re.sub(r'<[^>]*>', '<x>', path)
    return path.rstrip('/')


class ConsoleMeetUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client = web.app.test_client()
        r = client.get('/console')
        cls.status = r.status_code
        cls.html = r.get_data(as_text=True)
        cls.js = re.findall(r'<script>(.*?)</script>', cls.html, re.S)[-1]
        with open(CONTRACT, encoding='utf-8') as f:
            cls.doc = f.read()

    def test_console_renders_with_meet_chrome(self):
        self.assertEqual(self.status, 200)
        for el_id in ('tabs', 'tab-races', 'tab-meet', 'meet-cnt', 'meet-view', 'drawer', 'mpop',
                      'races-toolbar', 'races-layout', 'panes', 'dlg', 'ann-team-opts'):
            self.assertIn(f'id="{el_id}"', self.html, el_id)
        self.assertIn('href="#races"', self.html)
        self.assertIn('href="#meet"', self.html)

    def test_races_toolbar_and_meet_bar(self):
        # 2026-10-03: the Races toolbar keeps race actions; meet actions are on
        # the Meet tab's bar; Quick bib, Races from .evt and the legacy link are gone
        self.assertIn('+ New race', self.html)
        for text in ('Quick bib', 'Races from .evt', 'legacy console', 'href="/classic"', 'openFromEvt'):
            self.assertNotIn(text, self.html, text)
        self.assertRegex(self.js, r'function\s+meetBarActions\s*\(')
        for text in ('Export meet…', 'Close meet', 'openLoadMeet()'):
            self.assertIn(text, self.js, text)
        # meet-only buttons are drawn only while a meet is active
        self.assertIn('${MEET.active ? `<button class="btn" onclick="openDrawer(', self.js)

    def test_ui_pieces_present(self):
        for fn in ('meetApi', 'applyMeet', 'meetRoute', 'renderNoMeet', 'renderMeetInfo', 'athPage',
                   'openSwapPop', 'openWearerPop', 'walkupsPage', 'reviewRender', 'openBibDialog',
                   'racesLoad', 'teamsLoad', 'teamToggle', 'teamDetailLoad', 'teamDetailHtml', 'rostersDialog',
                   'openDrawer', 'announcerPrint', 'samePersonDlg'):
            self.assertRegex(self.js, r'(async\s+)?function\s+%s\s*\(' % fn, fn)
        self.assertIn("socket.on('meet_update'", self.js)
        for route in ('#meet/', "'info'", "'teams'", "'athletes'", "'races'"):
            self.assertIn(route, self.js)

    def test_api_paths_are_in_the_contract(self):
        doc_paths = {_norm(p) for p in re.findall(r'\|\s*(?:GET|POST|DELETE)\s*\|\s*(/api/meet[^\s|]*)', self.doc)}
        self.assertIn('/api/meet/bibs/<x>/wearer', doc_paths)          # parsed the tables
        js_paths = set(re.findall(r'''['"`](/api/meet(?:/[^'"`\s?]*)?)''', self.js))
        self.assertGreater(len(js_paths), 25)
        missing = []
        for p in sorted(js_paths):
            n = _norm(p)
            if n.startswith('/api/meet/print'):
                continue                                   # reports checked below
            if n not in doc_paths:
                missing.append(p)
        self.assertEqual(missing, [], 'console calls meet endpoints MEET-API.md does not name')

    def test_print_reports_are_in_the_contract(self):
        m = re.search(r'Reports:(.*?)Query:', self.doc, re.S)
        doc_reports = set(re.findall(r'`([a-z-]+)`', m.group(1)))
        self.assertIn('team-scores', doc_reports)
        used = set(re.findall(r"""(?:meetPrint|drawerPrint)\('([a-z-]+)'""", self.js))
        if 'announcerPrint' in self.js:
            used.add('announcer')
        self.assertGreater(len(used), 8)
        unknown = used - doc_reports - PENDING_REPORTS
        self.assertEqual(unknown, set(), 'reports the contract does not list')
        self.assertIn('/api/meet/print/${report}.${ext}', self.js)

    def test_print_params_mirror_the_server(self):
        # the server answers 400 to a param a report doesn't take, so the
        # console sends only these (PRINT_PARAMS in console.html)
        from rfid_finishlynx.meet_api import REPORTS
        m = re.search(r'const PRINT_PARAMS = \{(.*?)\};', self.js, re.S)
        self.assertIsNotNone(m)
        js_map = {k: set(re.findall(r"'([a-z_]+)'", v))
                  for k, v in re.findall(r"'([a-z-]+)':\s*\[([^\]]*)\]", m.group(1))}
        self.assertEqual(set(js_map), set(REPORTS))
        for name, (allowed, _race, _pdf) in REPORTS.items():
            self.assertEqual(js_map[name], set(allowed), name)

    def test_start_times_and_ui_gap_controls(self):
        """2026-10-03: start times (Races Start column, optional; Fill times… removed; the pane
        sub line, Schedule print, Send start times to Live) and the controls
        the UI build was missing (+ New race, Cancel race, + Add team, next
        free bib, scratched race, server-side walk-ups)."""
        for fn in ('raceTimeKey', 'raceTimeSave', 'raceNewDlg',
                   'raceNewSave', 'raceCancel', 'teamAddDlg', 'teamAddSave', 'liveStartTimes',
                   'walkupRecentLoad', 'paneStartText', 'printAllStartLists'):
            self.assertRegex(self.js, r'(async\s+)?function\s+%s\s*\(' % fn, fn)
        self.assertNotIn('Fill times', self.js)
        for path in ("'/api/meet/milesplit/live/start-times'",
                     "'/api/meet/athletes?source=local&limit=30'", "'/api/meet/bibs/next'",
                     "'/api/meet/teams', { name"):
            self.assertIn(path, self.js)
        self.assertIn("`/api/meet/races/${rk}`, undefined, 'DELETE'", self.js)     # Cancel race
        self.assertIn("{ start_time: val }", self.js)
        for text in ('+ New race', 'Cancel race…', '+ Add team',
                     'Send start times to Live', 'next free bib', 'next free: '):
            self.assertIn(text, self.js, text)
        self.assertIn("meetPrint('schedule', 'pdf')", self.js)
        # Print all start lists is ONE download (start-list without race)
        body = re.search(r'function printAllStartLists\(\) \{(.*?)\n\}', self.js, re.S).group(1)
        self.assertIn("meetPrint('start-list', 'pdf', {})", body)
        self.assertNotIn('race:', body)
        # scratches: the server's scratched_race, no per-browser memory
        self.assertIn('v.scratched_race', self.js)
        self.assertNotIn('SCRATCHED_IN', self.js)
        # the race pane's sub line carries the scheduled start
        self.assertEqual(self.js.count('${paneStartText(race)}'), 2)

    def test_script_parses(self):
        # Node first when it's installed (2026-10-09: an apostrophe inside a
        # '...' string broke the whole script - every button dead - and this
        # test only skipped, esprima not being installed).
        node = shutil.which('node')
        if node:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, 'console.js')
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(self.js)
                r = subprocess.run([node, '--check', path], capture_output=True, text=True,
                                   timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            return
        try:
            import esprima  # optional: pip install esprima
        except ImportError:
            self.skipTest('neither node nor esprima installed')
        # esprima 4 predates ?? and ?.: neutralise them for a syntax check
        esprima.parseScript(self.js.replace('??', '||').replace('?.', '.'))


if __name__ == '__main__':
    unittest.main()
