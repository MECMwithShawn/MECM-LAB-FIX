#!/usr/bin/env python3
"""Report whether given MiB offsets of a disk image fall in NTFS-free clusters.

Usage: ntfsfree.py IMAGE PART_START_BYTES < mib_offsets.txt
Reads $Bitmap (MFT record 6) of the NTFS volume at PART_START and prints, per input
MiB, how many of its clusters are in use.
"""
import struct
import sys

img, base = open(sys.argv[1], 'rb'), int(sys.argv[2])


def rd(off, n):
    img.seek(off)
    return img.read(n)


bs = rd(base, 512)
assert bs[3:11] == b'NTFS    '
bps, = struct.unpack_from('<H', bs, 11)
cluster = bps * bs[13]
mft_lcn, = struct.unpack_from('<Q', bs, 48)
rec_size = bs[64]
rec_size = cluster * rec_size if rec_size < 128 else 1 << (256 - rec_size)

rec = bytearray(rd(base + mft_lcn * cluster + 6 * rec_size, rec_size))
usa_off, usa_n = struct.unpack_from('<HH', rec, 4)
for i in range(1, usa_n):  # undo the update sequence fixups
    rec[i * bps - 2:i * bps] = rec[usa_off + 2 * i:usa_off + 2 * i + 2]

off, = struct.unpack_from('<H', rec, 20)
runs = None
while True:
    typ, length = struct.unpack_from('<II', rec, off)
    if typ == 0xFFFFFFFF:
        break
    if typ == 0x80 and rec[off + 8]:  # non-resident $DATA
        p = off + struct.unpack_from('<H', rec, off + 32)[0]
        runs, lcn = [], 0
        while rec[p]:
            ls, os_ = rec[p] & 15, rec[p] >> 4
            n = int.from_bytes(rec[p + 1:p + 1 + ls], 'little')
            lcn += int.from_bytes(rec[p + 1 + ls:p + 1 + ls + os_], 'little', signed=True)
            runs.append((lcn, n))
            p += 1 + ls + os_
        break
    off += length
assert runs, '$Bitmap data not found'
bitmap = b''.join(rd(base + l * cluster, n * cluster) for l, n in runs)

per_mib = (1 << 20) // cluster
used_any = 0
for line in sys.stdin:
    m = int(line)
    first = ((m << 20) - base) // cluster
    if first < 0:
        print(m, 'before partition')
        continue
    used = sum(bitmap[c >> 3] >> (c & 7) & 1 for c in range(first, first + per_mib))
    used_any += used > 0
    if used:
        print(f'MiB {m}: {used}/{per_mib} clusters IN USE')
print('MiBs with in-use clusters:', used_any)
