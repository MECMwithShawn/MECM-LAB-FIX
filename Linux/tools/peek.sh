#!/bin/bash
# peek.sh NAME : expose the Windows partition of disks/NAME.qcow2 at peek/NAME.part (read-only), print its IP config, then detach.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
cd "${LAB:-$HOME/vms/m365-lab}"; mkdir -p peek
n=$1
rm -f peek/$n.gpt; qemu-img dd if=disks/$n.qcow2 of=peek/$n.gpt bs=512 count=34 -O raw
read off end < <(python3 -I -c '
import sys,struct; t=open(sys.argv[1],"rb").read()[1024:]
best=max((struct.unpack_from("<QQ",t,i*128+32) for i in range(128)), key=lambda e:e[1]-e[0])
print(best[0]*512,(best[1]+1)*512)' peek/$n.gpt)
touch peek/$n.part
qemu-storage-daemon --blockdev driver=file,node-name=f,filename=disks/$n.qcow2,read-only=on --blockdev driver=qcow2,node-name=q,file=f,read-only=on \
  --blockdev driver=raw,node-name=p,file=q,offset=$off,size=$((end-off)),read-only=on \
  --export type=fuse,id=e,node-name=p,mountpoint=peek/$n.part,writable=off,allow-other=off --daemonize --pidfile peek/$n.pid
ntfscat peek/$n.part Windows/System32/config/SYSTEM > peek/$n-SYSTEM
kill $(cat peek/$n.pid); sleep 0.5; fusermount -u peek/$n.part 2>/dev/null || true
"${PYTHON:-.venv/bin/python}" "$HERE/netcfg.py" peek/$n-SYSTEM
