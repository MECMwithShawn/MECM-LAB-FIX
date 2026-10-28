#!/bin/bash
# Flatten each Hyper-V disk chain (base first, leaf last) to raw, then compact into qcow2.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
cd "${LAB:-$HOME/vms/m365-lab}"   # needs src/ (symlinks to the Hyper-V disks) and disks/
mkdir -p disks
flat() {  # name layer...
  local name=$1; shift
  [ -s disks/$name.qcow2 ] && { echo "$name: done already"; return; }
  echo "== $name $(date +%T)"
  python3 -I "$HERE/vhdxflat.py" disks/$name.raw "${@/#/src/}"
  qemu-img convert -O qcow2 disks/$name.raw disks/$name.qcow2.part
  mv disks/$name.qcow2.part disks/$name.qcow2
  rm -f "$PWD/disks/$name.raw"
  du -h disks/$name.qcow2
}
flat HYD-DC1 ServerParent.vhdx HYD-DC1.VHDX HYD-DC1_963882CE-F8D8-4EAB-A586-BA64C9DB0CDF.avhdx
flat HYD-GW1 ServerParent.vhdx HYD-GW1.VHDX HYD-GW1_0EA01D00-3304-4DE4-A4B2-D4DCC77757EB.avhdx HYD-GW1_6970DC21-D112-403F-A46E-06C7AC1676B7.avhdx
flat HYD-CM1 ServerParent.vhdx HYD-CM1.VHDX HYD-CM1_B6116A6A-825B-4C9C-846D-AE3E3A44BE26.avhdx HYD-CM1_6C23879C-4153-4029-AB9E-BFB9B6EB3B01.avhdx HYD-CM1_57CBF023-7E60-4340-AC8A-F990540A669A.avhdx HYD-CM1_1A82944E-E167-4296-AFA3-8F5251AB7CA6.avhdx
for n in 1 2 3 4; do flat HYD-CLIENT$n WindowsParent.vhdx HYD-CLIENT$n.VHDx; done
for n in 5 6; do [ -s disks/HYD-Client$n.qcow2 ] || qemu-img create -q -f qcow2 disks/HYD-Client$n.qcow2 120G; done
echo "ALL DONE $(date +%T)"
