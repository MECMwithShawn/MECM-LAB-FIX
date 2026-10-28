#!/bin/bash
# Add Lab Manager to the apps menu for the current user (no sudo). Rerun after moving the repo.
# Uses `sg libvirt` so it works even before a log-out/in has applied the libvirt group.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
icons=$HOME/.local/share/icons/hicolor/scalable/apps
apps=$HOME/.local/share/applications
mkdir -p "$icons" "$apps"
cp "$HERE/lab-manager.svg" "$icons/lab-manager.svg"
cat > "$apps/lab-manager.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Lab Manager
Comment=Start, stop and connect to the M365 deployment lab VMs
Exec=sg libvirt -c "python3 '$HERE/labmanager.py'"
Icon=lab-manager
Terminal=false
Categories=System;Emulator;
Keywords=vm;virtual;lab;hyper-v;kvm;libvirt;
StartupWMClass=lab-manager
EOF
gtk-update-icon-cache -q -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$apps" 2>/dev/null || true
echo "Lab Manager added to the apps menu ($apps/lab-manager.desktop)"
