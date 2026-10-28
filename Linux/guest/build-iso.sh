#!/bin/bash
# Build $LAB/lab-fixup.iso (volume label LABFIX): the guest scripts in this folder plus the
# virtio-win guest tools (SPICE agent for clipboard/resize, QEMU guest agent, drivers).
# Every lab VM has this ISO in its CD drive (usually D:).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
LAB=${LAB:-$HOME/vms/m365-lab}
TOOLS_URL=https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.302-1/virtio-win-guest-tools.exe
TOOLS_SHA256=d8ae9ea1e943384ac195e012cd36f82c3161620e5fc9529be4e42e6f420e222d

mkdir -p "$LAB/downloads" "$LAB/iso"
exe=$LAB/downloads/virtio-win-guest-tools.exe
[ -f "$exe" ] || curl -sSfL -o "$exe" "$TOOLS_URL"
echo "$TOOLS_SHA256  $exe" | sha256sum -c --quiet

cp "$HERE"/*.ps1 "$exe" "$LAB/iso/"
genisoimage -quiet -J -r -V LABFIX -o "$LAB/lab-fixup.iso.new" "$LAB/iso/"
mv "$LAB/lab-fixup.iso.new" "$LAB/lab-fixup.iso"
echo "built $LAB/lab-fixup.iso"
# Running VMs only notice a new ISO after an eject + insert:
#   virsh -c qemu:///system change-media VM sdb --eject --live
#   virsh -c qemu:///system change-media VM sdb "$LAB/lab-fixup.iso" --insert --live
