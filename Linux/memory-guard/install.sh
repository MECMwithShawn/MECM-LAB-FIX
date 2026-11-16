#!/bin/bash
# Install the VM memory guard for the current user (no sudo) and start it.
# Rerun after pulling changes. Remove with: ./install.sh --remove
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
dest=$HOME/.local/share/vm-memory-guard
unit=$HOME/.config/systemd/user/vm-memory-guard.service

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now vm-memory-guard.service 2>/dev/null || true
    rm -f "$unit"; rm -rf "$dest"
    systemctl --user daemon-reload
    echo "VM memory guard removed"
    exit 0
fi

mkdir -p "$dest" "$(dirname "$unit")"
install -m 755 "$HERE/vm-memory-guard.py" "$HERE/warning-window.py" "$dest/"
install -m 644 "$HERE/blank.svg" "$dest/"
cat > "$unit" <<UNIT
[Unit]
Description=VM memory guard: warn and pause lab client VMs when memory runs low
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 %h/.local/share/vm-memory-guard/vm-memory-guard.py
Restart=on-failure
RestartSec=10
# Stay alive and responsive when memory is tight.
OOMScoreAdjust=-900

[Install]
WantedBy=graphical-session.target
UNIT
systemctl --user daemon-reload
systemctl --user enable vm-memory-guard.service
systemctl --user restart vm-memory-guard.service
echo "VM memory guard installed and running ($dest)"
if ! swapon --noheadings --show | grep -q .; then
    echo "Note: this machine has no swap. See README.md, 'Add swap first'."
fi
