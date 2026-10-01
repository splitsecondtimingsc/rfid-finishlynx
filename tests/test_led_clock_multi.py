"""Multi-clock LED service: several adapters driven at once, per-clock status
for the console chips, settings that follow a clock across COM numbers.
unittest-style so it runs without pytest."""

import time
import unittest
from datetime import datetime

import serial

from rfid_finishlynx.led_clock import LEDClockService, find_clocks


class FakeDriver:
    kind = "fake"

    def __init__(self, port):
        self.port = port
        self.driven = []
        self.closed = False
        self.fail_next = False

    def drive(self, gun, wait):
        if self.fail_next:
            self.fail_next = False
            raise serial.SerialException("unplugged")
        self.driven.append(gun)
        return 0.01

    def close(self):
        self.closed = True


class Harness:
    """found: list of (kind, port) tuples or {port, kind, serial} dicts."""

    def __init__(self, found, busy_ports=(), clocks=None, get_gun=lambda: None,
                 ch340_ports=(), exclude=None):
        self.found = list(found)
        self.busy = set(busy_ports)
        self.ch340_ports = set(ch340_ports)   # ports that answer the CH340 probe
        self.probed = []
        self.drivers = {}
        self.saved = 0
        self.entries = clocks if clocks is not None else []

        def factory(kind, port):
            if port in self.busy:
                raise serial.SerialException(
                    f"could not open port '{port}': PermissionError(13, 'Access is denied.')")
            if port not in self.present_ports():
                raise serial.SerialException(
                    f"could not open port '{port}': FileNotFoundError(2, 'The system cannot find the file specified.')")
            d = FakeDriver(port)
            d.kind = kind
            self.drivers[port] = d
            return d

        def probe(port):
            self.probed.append(port)
            return port in self.ch340_ports

        def on_change(entries):
            self.saved += 1

        self.svc = LEDClockService(get_gun, clocks=self.entries, driver_factory=factory,
                                   find_clocks_fn=lambda t, p: list(self.found),
                                   probe_fn=probe, exclude_ports=exclude, on_change=on_change)

    def present_ports(self):
        return {f["port"] if isinstance(f, dict) else f[1] for f in self.found}


def statuses(svc):
    return {c["port"]: c["status"] for c in svc.clocks}


class TestMultiClock(unittest.TestCase):

    def test_two_adapters_both_driven(self):
        gun = datetime.now()
        h = Harness([("ch340", "COM7"), ("electro", "COM5")], get_gun=lambda: gun)
        self.assertTrue(h.svc._connect())
        self.assertEqual(set(h.drivers), {"COM5", "COM7"})
        self.assertEqual(h.svc._drive_all(), 0.01)
        for d in h.drivers.values():
            self.assertEqual(d.driven, [gun])
        self.assertEqual(statuses(h.svc), {"COM5": "driving", "COM7": "driving"})
        self.assertTrue(h.svc.is_connected)
        self.assertIn(h.svc.clock_kind, ("ch340", "electro"))

    def test_busy_port_reports_amber_and_others_still_drive(self):
        h = Harness([("ch340", "COM7"), ("electro", "COM5")], busy_ports={"COM5"})
        self.assertTrue(h.svc._connect())
        by_port = {c["port"]: c for c in h.svc.clocks}
        self.assertEqual(by_port["COM7"]["status"], "driving")
        self.assertEqual(by_port["COM5"]["status"], "busy")
        self.assertIn("denied", by_port["COM5"]["error"])
        self.assertEqual(by_port["COM5"]["label"], "Electro clock COM5")

    def test_configured_clock_missing_shows_red_with_label(self):
        h = Harness([], clocks=[{"port": "COM9", "type": "electro", "label": "Mile 2"}])
        self.assertFalse(h.svc._connect())
        (c,) = h.svc.clocks
        self.assertEqual((c["port"], c["label"], c["status"], c["configured"]),
                         ("COM9", "Mile 2", "missing", True))
        h.found.append(("electro", "COM9"))
        self.assertTrue(h.svc._connect())
        (c,) = h.svc.clocks
        self.assertEqual((c["label"], c["status"], c["kind"]), ("Mile 2", "driving", "electro"))

    def test_label_follows_clock_to_a_new_com_number(self):
        """The Electro clock was COM5 last meet; today it enumerates as COM9.
        Same type, old port absent -> the entry (and its label) move over,
        and the config is updated so next time COM9 is the first guess."""
        entries = [{"port": "COM5", "type": "electro", "label": "Finish"}]
        h = Harness([("electro", "COM9"), ("ch340", "COM12")], clocks=entries)
        h.svc._connect()
        by_port = {c["port"]: c for c in h.svc.clocks}
        self.assertEqual(set(by_port), {"COM9", "COM12"})          # no phantom COM5
        self.assertEqual(by_port["COM9"]["label"], "Finish")
        self.assertEqual(by_port["COM9"]["status"], "driving")
        self.assertEqual(entries[0]["port"], "COM9")
        self.assertGreaterEqual(h.saved, 1)

    def test_serial_number_match_beats_type_match(self):
        entries = [{"port": "COM3", "type": "ch340", "label": "Mile 1", "serial": "AAA"},
                   {"port": "COM4", "type": "ch340", "label": "Mile 2", "serial": "BBB"}]
        # Both renumbered, and in swapped order.
        h = Harness([{"port": "COM8", "kind": "ch340", "serial": "BBB"},
                     {"port": "COM9", "kind": "ch340", "serial": "AAA"}], clocks=entries)
        h.svc._connect()
        by_port = {c["port"]: c["label"] for c in h.svc.clocks}
        self.assertEqual(by_port, {"COM8": "Mile 2", "COM9": "Mile 1"})

    def test_unknown_adapter_probed_and_identified_as_ch340(self):
        h = Harness([{"port": "COM20", "kind": None, "serial": "X1"}], ch340_ports={"COM20"})
        self.assertTrue(h.svc._connect())
        self.assertEqual(h.probed, ["COM20"])
        (c,) = h.svc.clocks
        self.assertEqual((c["kind"], c["status"]), ("ch340", "driving"))

    def test_unknown_adapter_that_stays_silent_is_listed_not_driven(self):
        h = Harness([{"port": "COM20", "kind": None, "serial": None}])
        self.assertFalse(h.svc._connect())
        (c,) = h.svc.clocks
        self.assertEqual((c["kind"], c["status"], c["label"]), ("unknown", "unknown", "COM20 (unidentified)"))
        self.assertNotIn("COM20", h.drivers)
        # Not re-probed on the very next pass (rate limited)...
        h.svc._connect()
        self.assertEqual(h.probed, ["COM20"])
        # ...but the operator can type it, and then it is driven and remembered.
        h.svc.update_slot("COM20", kind="electro", label="Mile 2")
        h.svc._connect()
        (c,) = h.svc.clocks
        self.assertEqual((c["kind"], c["status"], c["label"]), ("electro", "driving", "Mile 2"))
        self.assertEqual(h.entries[0]["type"], "electro")

    def test_not_a_clock_hides_port_and_persists(self):
        h = Harness([{"port": "COM20", "kind": None, "serial": None}, ("electro", "COM5")])
        h.svc._connect()
        h.svc.update_slot("COM20", kind="none")
        h.svc._connect()
        self.assertEqual([c["port"] for c in h.svc.clocks], ["COM5"])
        self.assertEqual(h.entries, [{"port": "COM20", "type": "none"}])
        # Fresh service from the same config keeps ignoring it.
        h2 = Harness([{"port": "COM20", "kind": None, "serial": None}, ("electro", "COM5")],
                     clocks=list(h.entries))
        h2.svc._connect()
        self.assertEqual([c["port"] for c in h2.svc.clocks], ["COM5"])
        self.assertEqual(h2.probed, [])

    def test_excluded_port_never_touched(self):
        h = Harness([("ch340", "COM4"), ("electro", "COM5")], exclude=["COM4"])
        h.svc._connect()
        self.assertEqual([c["port"] for c in h.svc.clocks], ["COM5"])

    def test_serial_error_drops_only_that_clock(self):
        h = Harness([("ch340", "COM7"), ("electro", "COM5")])
        h.svc._connect()
        h.drivers["COM5"].fail_next = True
        self.assertEqual(h.svc._drive_all(), 0.01)  # COM7 still returns its delay
        st = statuses(h.svc)
        self.assertEqual(st["COM7"], "driving")
        self.assertNotEqual(st.get("COM5"), "driving")
        h.found = [("ch340", "COM7")]
        h.svc._connect()
        self.assertEqual([c["port"] for c in h.svc.clocks], ["COM7"])

    def test_busy_adapter_that_vanishes_is_forgotten(self):
        h = Harness([("ch340", "COM11")], busy_ports={"COM11"})
        h.svc._connect()
        self.assertEqual([c["status"] for c in h.svc.clocks], ["busy"])
        h.found = [("ch340", "COM12")]
        h.svc._connect()
        self.assertEqual([(c["port"], c["status"]) for c in h.svc.clocks], [("COM12", "driving")])

    def test_disable_closes_and_stops_retrying(self):
        h = Harness([("ch340", "COM7"), ("electro", "COM5")])
        h.svc._connect()
        kind = h.svc.update_slot("COM5", enabled=False)
        self.assertEqual(kind, "electro")
        self.assertTrue(h.drivers["COM5"].closed)
        by_port = {c["port"]: c for c in h.svc.clocks}
        self.assertEqual(by_port["COM5"]["status"], "disabled")
        self.assertTrue(by_port["COM5"]["configured"])
        del h.drivers["COM5"]
        h.svc._connect()
        self.assertNotIn("COM5", h.drivers)
        h.svc.update_slot("COM5", enabled=True)
        h.svc._connect()
        self.assertIn("COM5", h.drivers)

    def test_label_and_retry(self):
        h = Harness([("ch340", "COM7")])
        h.svc._connect()
        h.svc.update_slot("COM7", label="Finish")
        (c,) = h.svc.clocks
        self.assertEqual(c["label"], "Finish")
        self.assertEqual(h.entries[0]["label"], "Finish")
        first = h.drivers["COM7"]
        h.svc.retry("COM7")
        self.assertTrue(first.closed)
        h.svc._connect()
        self.assertIsNot(h.drivers["COM7"], first)

    def test_configured_order_first_then_discovered(self):
        h = Harness([("ch340", "COM3"), ("electro", "COM1")],
                    clocks=[{"port": "COM9", "label": "Finish"}])
        h.svc._connect()
        self.assertEqual([c["port"] for c in h.svc.clocks], ["COM9", "COM1", "COM3"])

    def test_legacy_single_finder_still_works(self):
        made = []

        def factory(kind, port):
            d = FakeDriver(port)
            made.append(d)
            return d

        svc = LEDClockService(lambda: None, driver_factory=factory,
                              find_clock_fn=lambda t, p: ("fake", "/dev/fake0"))
        self.assertTrue(svc._connect())
        self.assertEqual([d.port for d in made], ["/dev/fake0"])
        svc.stop()
        self.assertTrue(made[0].closed)

    def test_loop_drives_everything_and_stop_closes(self):
        gun = datetime.now()
        h = Harness([("ch340", "COM7"), ("electro", "COM5")], get_gun=lambda: gun)
        h.svc.start()
        deadline = time.time() + 2.0
        while (len(h.drivers) < 2 or any(len(d.driven) < 3 for d in h.drivers.values())) \
                and time.time() < deadline:
            time.sleep(0.01)
        h.svc.stop()
        self.assertEqual(len(h.drivers), 2)
        for d in h.drivers.values():
            self.assertGreaterEqual(len(d.driven), 3)
            self.assertTrue(d.closed)

    def test_find_clocks_explicit_port(self):
        self.assertEqual(find_clocks("electro", "COM5"), [("electro", "COM5")])
        self.assertEqual(find_clocks("auto", "COM5"), [("ch340", "COM5")])


if __name__ == "__main__":
    unittest.main()
