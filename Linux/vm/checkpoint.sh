#!/bin/bash
# Hyper-V style checkpoints for the lab VMs (libvirt can't snapshot a running UEFI VM).
# The VM must be shut off; the snapshot is stored inside its qcow2 disk.
#   checkpoint.sh list   HYD-CLIENT4
#   checkpoint.sh create HYD-CLIENT4 [name]     (default name: date-time)
#   checkpoint.sh revert HYD-CLIENT4 [name]     (default: latest)
#   checkpoint.sh delete HYD-CLIENT4 name
set -euo pipefail
cmd=${1:?list|create|revert|delete} vm=${2:?VM name}
disk=${LAB:-$HOME/vms/m365-lab}/disks/$vm.qcow2
[ -f "$disk" ] || { echo "no disk $disk"; exit 1; }

if [ "$cmd" != list ]; then
  state=$(sg libvirt -c "virsh -c qemu:///system domstate $vm")
  [ "$state" = "shut off" ] || { echo "$vm is $state; shut it down first (checkpoints need it off)"; exit 1; }
fi

case $cmd in
  list)   qemu-img snapshot -U -l "$disk" ;;
  create) name=${3:-$(date +%Y-%m-%d_%H%M)}; qemu-img snapshot -c "$name" "$disk"; echo "created checkpoint '$name' on $vm" ;;
  revert) name=${3:-$(qemu-img snapshot -l "$disk" | awk 'NR>2 {n=$2} END {print n}')}
          [ -n "$name" ] || { echo "$vm has no checkpoints"; exit 1; }
          qemu-img snapshot -a "$name" "$disk"; echo "reverted $vm to '$name'" ;;
  delete) qemu-img snapshot -d "${3:?checkpoint name}" "$disk"; echo "deleted '$3' from $vm" ;;
  *) echo "unknown command $cmd"; exit 1 ;;
esac
