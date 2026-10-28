#!/bin/bash
# shot.sh VM OUT.png [ffmpeg-filter] : screenshot a VM (needs libvirt group)
tmp=$(mktemp --suffix=.ppm)
sg libvirt -c "virsh -c qemu:///system screenshot $1 $tmp" >/dev/null
ffmpeg -loglevel error -y -i $tmp -vf "${3:-scale=960:-1}" "$2"
rm -f "$tmp"
