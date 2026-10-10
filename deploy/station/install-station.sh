#!/bin/bash
# Install or update a split / check-in station on a Raspberry Pi.
# Run from the unpacked rfid_station folder, as the user the station runs as:
#     ./install-station.sh            rfid-remote runs at boot
#     ./install-station.sh --beacon   the range-test beacon runs at boot instead
# Needs internet once (pip), unless a wheels/ folder sits next to this script
# (an SD card built for the timing net). Safe to run again after unpacking a
# newer build.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
USER_NAME=$(id -un)
cd "$DIR"

if [ -d wheels ]; then
    PIP_FROM=(--no-index --find-links wheels)
    python3 -m venv venv 2>/dev/null || {
        # no python3-venv and no internet: bootstrap pip from its wheel
        python3 -m venv --clear --without-pip venv
        venv/bin/python wheels/pip-*.whl/pip install --no-index wheels/pip-*.whl; }
else
    PIP_FROM=()
    python3 -m venv venv 2>/dev/null || { sudo apt-get install -y python3-venv; python3 -m venv venv; }
fi
venv/bin/python -m pip install "${PIP_FROM[@]}" -r requirements.txt

# Keep the settings of a station installed the old way (whole repo in ~/rfid_finishlynx)
if [ ! -f config.yaml ]; then
    if [ -f "$HOME/rfid_finishlynx/config.yaml" ]; then
        cp "$HOME/rfid_finishlynx/config.yaml" config.yaml
        echo "config.yaml copied from $HOME/rfid_finishlynx"
    else
        cp config.example.yaml config.yaml
        echo "config.yaml created - set lora.port and lora.reader_id"
    fi
fi

# Serial radio and LED clocks
sudo usermod -aG dialout "$USER_NAME"
# The station steps its clock to the finish line's (sudo -n date, app.py)
echo "$USER_NAME ALL=(root) NOPASSWD: $(command -v date)" | sudo tee /etc/sudoers.d/rfid-station >/dev/null
sudo chmod 440 /etc/sudoers.d/rfid-station
sudo visudo -cf /etc/sudoers.d/rfid-station >/dev/null

for svc in rfid-remote rfid-range-beacon; do
    sed -e "s|@DIR@|$DIR|g" -e "s|@USER@|$USER_NAME|g" "$svc.service" \
        | sudo tee "/etc/systemd/system/$svc.service" >/dev/null
done
sudo systemctl daemon-reload
if [ "${1:-}" = "--beacon" ]; then
    sudo systemctl disable --now rfid-remote 2>/dev/null || true
    sudo systemctl enable --now rfid-range-beacon
else
    sudo systemctl disable --now rfid-range-beacon 2>/dev/null || true
    sudo systemctl enable --now rfid-remote
    sudo systemctl restart rfid-remote
fi
systemctl --no-pager --lines=0 status rfid-remote rfid-range-beacon || true
