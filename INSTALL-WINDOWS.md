# Windows Install — RFID FinishLynx Middleware

Target: the finish-line timing PC (same machine as FinishLynx).

## The short version

1. Unzip the package anywhere (straight off the USB stick is fine).
2. Double-click **`INSTALL.bat`**, accept the admin prompt, wait.
3. Done. The console opens at **http://localhost:8080/**.

The installer needs **no internet** — Python 3.12 and every package are
bundled. It installs to `C:\rfid_finishlynx` and sets up:

- Python 3.12 (skipped if already installed)
- all Python dependencies (offline, from `wheels\`)
- both LED-clock USB-serial drivers (CH340 + PL2303)
- meet sharing with your other PCs: a folder in **Google Drive for
  desktop** (`My Drive\RFID meets`, asked when Drive is installed; or later
  in the console, Meet > Info > Sharing)
- an **"RFID Console"** shortcut on the Desktop (starts the middleware
  server) and the same shortcut in Startup (auto-starts at logon)
- then launches the console and opens the web page

Re-running `INSTALL.bat` is safe — it re-copies files and skips what's
already installed, so it doubles as the upgrade path for a new package.

Then connect FinishLynx: see **FINISHLYNX-SETUP.md** (LapTime plug-in on
TCP 5000; scoreboard output to UDP 127.0.0.1:5891).

## Configuration (`config.yaml` in C:\rfid_finishlynx)

The shipped config works for the current hardware out of the box:

- `reader:` — Vanch VF-747P at its factory `192.168.0.128:502`. If a
  different reader (or an Impinj) is plugged in instead, the supervisor
  auto-detects and adopts it, rewriting this section itself.
- `finishlynx: host 127.0.0.1, port 5000` — FinishLynx on the same PC.
- `scoreboard: port 5891` — UDP listener for FinishLynx output.
- `races: lif_dir` — set to MeetPro's F.A.T. data directory to stream
  .lif results (or just load a meet folder from the console UI).
- `reader.antennas` — **the reader ports your antennas are plugged into**
  (the VF-787P has 8; this rig uses `[1, 3, 5, 7]`). On a Vanch the
  middleware pushes this list to the reader's automatic-polling antenna set
  (VUP command 15H over UDP 1969) every time it connects. A wrong list
  means the reader silently ignores the other antennas - on 2026-09-12 the
  reader had been polling port 1 alone, which was the "missing reads".
  Edit it from the Reader chip popover (tick exactly the used ports) or in
  config.yaml. Vendor protocol manuals are in `docs/vanch/`.
- `lora:` — disabled by default here; enable and set the gateway
  address when the split-station radios are attached to this machine.
- `led_clock:` — enabled; scans every USB serial port on each pass, so
  the COM number does not matter and can change between meets. A CH340
  adapter is the newer LED clock, a PL2303 is the Electro Numerics; any
  other USB-serial adapter is asked for a clock reply and, if silent,
  listed as "(unidentified)" so you can pick its type from the chip
  popover. Every identified clock is driven with the same race time.
  The console shows one chip per clock: green = driving, amber = adapter
  present but the COM port is held by something else (FinishLynx's own
  scoreboard entry, usually), red = configured but unplugged, grey =
  unidentified or disabled. Labels/types set in the popover are saved and
  follow the clock (matched by USB serial number when the adapter has one,
  otherwise by clock type) when it lands on a different COM port. The
  saved form looks like:

  ```yaml
  led_clock:
    enabled: true
    port: auto
    type: auto
    clocks:
      - {port: COM11, type: ch340, label: Finish}
      - {port: COM5, type: electro, label: Mile 2}
  ```

  Unlisted adapters are still driven; they just get a generic label.

Settings are also editable live in the web UI (`/classic` → Settings).

## Optional: run the test suite

```
cd C:\rfid_finishlynx
python -m pip install pytest
python -m pytest tests -q
```

All tests should pass (they use fake hardware; nothing needs plugging in).

## Troubleshooting

- **Install window closed instantly** — right-click `INSTALL.bat` →
  Run as administrator (the self-elevation prompt may have been
  dismissed).
- **Reader chip red in the console** — reader powered? (Vanch needs its
  12 V brick or a PoE injector.) Ethernet on the `192.168.0.x` subnet?
- **LED clock dark** — Device Manager should show the USB-serial port
  without a warning icon (else see `docs\ledclock\old-electro\DRIVER.md`
  — counterfeit PL2303 cables show Code 10). The Electro clock must
  also be in "rd" mode on the clock itself.
- **Meet-day operations** — `MEET-DAY.md`.

## Manual install (fallback)

If `INSTALL.bat` can't be used: install Python 3.11/3.12 from
python.org (check "Add to PATH"), unzip to `C:\rfid_finishlynx`, run
`python -m pip install --no-index --find-links wheels -r requirements.txt`,
then use `deploy\start-console.bat` (put a shortcut in `shell:startup`),
and if a clock needs drivers run `deploy\install-clock-drivers.bat` as
administrator.
