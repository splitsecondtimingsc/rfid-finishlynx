"""2026-10-04: the Raspberry Pi station package (tools/build_station.py).

- only what a remote_lap station runs is in it, plus the installer,
  services, station config and requirements at the top;
- unpacked on its own it starts as a station and serves the dashboard:
  no meet / race / FinishLynx-PC module is ever imported, and every GET
  route answers except the finish console's setup and update checks;
- every third-party import in it is in the station requirements.

Hardware-free: python -m unittest tests.test_station_package
"""
import ast
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_station  # noqa: E402

FINISH_ONLY_ROUTES = {"/api/setup_check", "/api/update/check"}
# import name -> requirements line; zeroconf is optional (avahi first on Linux)
REQUIREMENT = {"flask": "flask", "flask_socketio": "flask-socketio", "paramiko": "paramiko",
               "serial": "pyserial", "sllurp": "sllurp", "yaml": "pyyaml"}
OPTIONAL = {"zeroconf"}

SMOKE = r'''
import json, os, sys
os.environ["RFID_PREVIEW"] = "1"
sys.path.insert(0, os.getcwd())
from rfid_finishlynx.config import load_config
from rfid_finishlynx.app import Application
from rfid_finishlynx import web
cfg = load_config("config.yaml")
app = Application(cfg, "config.yaml")
web.set_application(app)
app.start()
web.app.testing = False
c = web.app.test_client()
failed = []
for rule in web.app.url_map.iter_rules():
    if "GET" in rule.methods and not rule.arguments and \
            not rule.rule.startswith(("/static", "/api/discover", "/api/browse")):
        if c.get(rule.rule).status_code >= 500:
            failed.append(rule.rule)
out = {"mode": cfg.mode, "failed": failed, "meet": app.meet_service is not None,
       "races": app.race_manager is not None,
       "home": "RFID Timing Control" in c.get("/").get_data(as_text=True),
       "modules": sorted(m for m in sys.modules if m.startswith("rfid_finishlynx"))}
app.shutdown()
print("RESULT " + json.dumps(out), flush=True)
os._exit(0)
'''


# Hardware allowed (as on the Pi) but never started: nothing is opened, and
# the status the dashboard polls must not reach finish-line-only modules.
STATUS_REAL = r'''
import json, os, sys
sys.path.insert(0, os.getcwd())
from rfid_finishlynx.config import load_config
from rfid_finishlynx.app import Application
from rfid_finishlynx import web
app = Application(load_config("config.yaml"), "config.yaml")
web.set_application(app)
web.app.testing = False
r = web.app.test_client().get("/api/status")
print("RESULT " + json.dumps({"allowed": app.hardware_allowed, "status": r.status_code}), flush=True)
os._exit(0)
'''


@unittest.skipUnless(os.path.isdir(os.path.join(ROOT, ".git")), "builds from git (not in a hand-out install)")
class TestStationPackage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = build_station.build(cls.tmp.name, worktree=True)
        with tarfile.open(out) as tar:
            cls.members = {m.name: m for m in tar.getmembers()}
            tar.extractall(cls.tmp.name, filter="data")
        cls.dir = os.path.join(cls.tmp.name, "rfid_station")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_contents(self):
        names = set(self.members)
        for top in ("run.py", "range_test.py", "setup_gateway.py", "test_lora_link.py", "install-station.sh", "rfid-remote.service",
                    "rfid-range-beacon.service", "config.example.yaml", "requirements.txt",
                    "VERSION.txt", "rfid_finishlynx/app.py", "rfid_finishlynx/templates/index.html"):
            self.assertIn("rfid_station/" + top, names)
        for dropped in build_station.DROP:
            self.assertFalse(any(n.startswith("rfid_station/" + dropped) for n in names), dropped)
        self.assertFalse(any(n.startswith(("rfid_station/tests", "rfid_station/docs",
                                           "rfid_station/python/", "rfid_station/wheels"))
                             for n in names))
        self.assertTrue(self.members["rfid_station/install-station.sh"].mode & 0o111)

    def test_runs_as_a_station(self):
        with open(os.path.join(self.dir, "config.example.yaml")) as src, \
                open(os.path.join(self.dir, "config.yaml"), "w") as dst:
            dst.write(src.read())
        proc = subprocess.run([sys.executable, "-c", SMOKE], cwd=self.dir,
                              capture_output=True, text=True, timeout=120)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        self.assertIsNotNone(line, proc.stderr[-2000:])
        import json
        out = json.loads(line[len("RESULT "):])
        self.assertEqual(out["mode"], "remote_lap")
        self.assertFalse(out["meet"])
        self.assertFalse(out["races"])
        self.assertTrue(out["home"])
        self.assertEqual(set(out["failed"]) - FINISH_ONLY_ROUTES, set())
        dropped = {d[:-3].replace("/", ".") for d in build_station.DROP if d.endswith(".py")}
        self.assertEqual(dropped & set(out["modules"]), set())

    def test_status_with_hardware_allowed(self):
        with open(os.path.join(self.dir, "config.example.yaml")) as src, \
                open(os.path.join(self.dir, "config.yaml"), "w") as dst:
            dst.write(src.read())
        env = {k: v for k, v in os.environ.items() if k != "RFID_PREVIEW"}
        env["RFID_HW_LOCK"] = os.path.join(self.tmp.name, "hw.lock")
        proc = subprocess.run([sys.executable, "-c", STATUS_REAL], cwd=self.dir, env=env,
                              capture_output=True, text=True, timeout=120)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        self.assertIsNotNone(line, proc.stderr[-2000:])
        import json
        out = json.loads(line[len("RESULT "):])
        self.assertTrue(out["allowed"])
        self.assertEqual(out["status"], 200, proc.stderr[-2000:])

    def test_requirements_cover_imports(self):
        with open(os.path.join(self.dir, "requirements.txt")) as f:
            reqs = {l.split(">")[0].split("=")[0].strip().lower() for l in f if l.strip()}
        for path, member in self.members.items():
            if not path.endswith(".py"):
                continue
            with open(os.path.join(self.tmp.name, path), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    if top in sys.stdlib_module_names or top == "rfid_finishlynx" or top in OPTIONAL:
                        continue
                    self.assertIn(REQUIREMENT.get(top, top), reqs, f"{path} imports {top}")


if __name__ == "__main__":
    unittest.main()
