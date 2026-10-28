#!/bin/bash
# Define the two lab networks and the nine lab VMs in libvirt (qemu:///system).
# Needs the converted disks in $LAB/disks and $LAB/lab-fixup.iso (guest/build-iso.sh).
# Safe to rerun: existing networks are left alone, VMs are redefined in place.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
export LAB=${LAB:-$HOME/vms/m365-lab}
V='virsh -c qemu:///system'

for net in corpnet internet; do
  name=$(sed -n 's|.*<name>\(.*\)</name>.*|\1|p' "$HERE/net-$net.xml")
  if ! $V net-info "$name" >/dev/null 2>&1; then
    $V net-define "$HERE/net-$net.xml"
    $V net-autostart "$name"
    $V net-start "$name"
  fi
done

python3 -I "$HERE/make-vms.py"
for x in "$LAB"/xml/HYD-*.xml; do
  n=$(basename "$x" .xml)
  # keep the existing UUID on a redefine, or libvirt refuses it
  if uuid=$($V domuuid "$n" 2>/dev/null); then
    sed -i "s|  <name>$n</name>|  <name>$n</name>\n  <uuid>$uuid</uuid>|" "$x"
  fi
  $V define "$x" >/dev/null && echo "defined $n"
done
