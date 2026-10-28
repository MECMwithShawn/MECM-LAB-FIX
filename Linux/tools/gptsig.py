#!/usr/bin/env python3
"""Print GPT partitions of a raw disk image with their filesystem signature (NTFS / BitLocker / FAT)."""
import struct
import sys

path = sys.argv[1]
with open(path, 'rb') as f:
    f.seek(512)
    hdr = f.read(92)
    assert hdr[:8] == b'EFI PART', 'no GPT'
    lba, count, size = struct.unpack_from('<QII', hdr, 72)
    f.seek(lba * 512)
    table = f.read(count * size)
    for i in range(count):
        e = table[i * size:(i + 1) * size]
        if e[:16] == b'\0' * 16:
            continue
        first, last = struct.unpack_from('<QQ', e, 32)
        name = e[56:128].decode('utf-16-le').rstrip('\0')
        f.seek(first * 512)
        boot = f.read(16)
        sig = boot[3:11]
        kind = {b'NTFS    ': 'NTFS', b'-FVE-FS-': 'BITLOCKER', b'MSDOS5.0': 'FAT'}.get(sig, sig)
        print(f'{i + 1}: {(last - first + 1) * 512 / 2**30:7.2f} GiB  {kind}  {name}')
