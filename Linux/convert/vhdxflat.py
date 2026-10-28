#!/usr/bin/env python3
"""Flatten a Hyper-V VHDX/AVHDX differencing chain into a sparse raw image.

Usage: vhdxflat.py OUT.raw BASE.vhdx [CHILD.avhdx ...]   (base first, leaf last)

Only allocated payload blocks are read, so empty space costs nothing. Each layer
is applied on top of the previous one: fully-present blocks are copied, partially-
present blocks are copied sector by sector using the layer's sector bitmap, and
zero/unmapped blocks punch a hole in the output.
"""
import ctypes
import os
import struct
import sys
import uuid

MB = 1 << 20
BAT_GUID = uuid.UUID('2DC27766-F623-4200-9D64-115E9BFD4A08')
META_GUID = uuid.UUID('8B7CA206-4790-4B9A-B8FE-575F050F886E')
FILE_PARAMS = uuid.UUID('CAA16737-FA36-4D43-B3B6-33F0AA44E76B')
VDISK_SIZE = uuid.UUID('2FA54224-CD1B-4876-B211-5DBED83BF4B8')
LOGICAL_SECTOR = uuid.UUID('8141BF1D-A96F-4709-BA47-F233A8FAAB5F')

NOT_PRESENT, UNDEFINED, ZERO, UNMAPPED, FULLY_PRESENT, PARTIALLY_PRESENT = 0, 1, 2, 3, 6, 7

libc = ctypes.CDLL(None, use_errno=True)
libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_long, ctypes.c_long]


def punch(fd, off, length):
    if libc.fallocate(fd, 0x01 | 0x02, off, length) != 0:  # KEEP_SIZE | PUNCH_HOLE
        raise OSError(ctypes.get_errno(), 'fallocate punch hole failed')


class Vhdx:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        assert self.read(0, 8) == b'vhdxfile', f'{path}: not a VHDX'
        heads = []
        for off in (64 * 1024, 128 * 1024):
            h = self.read(off, 4096)
            if h[:4] == b'head':
                seq, = struct.unpack_from('<Q', h, 8)
                heads.append((seq, h))
        _, head = max(heads)
        if uuid.UUID(bytes_le=head[48:64]).int != 0:
            raise SystemExit(f'{path}: log is not empty (disk was not closed cleanly); open it in Hyper-V once first')
        regi = self.read(192 * 1024, 64 * 1024)
        assert regi[:4] == b'regi'
        count, = struct.unpack_from('<I', regi, 8)
        regions = {}
        for i in range(count):
            e = regi[16 + 32 * i:48 + 32 * i]
            guid = uuid.UUID(bytes_le=e[:16])
            off, length = struct.unpack_from('<QI', e, 16)
            regions[guid] = (off, length)

        moff, _ = regions[META_GUID]
        mt = self.read(moff, 64 * 1024)
        assert mt[:8] == b'metadata'
        n, = struct.unpack_from('<H', mt, 10)
        items = {}
        for i in range(n):
            e = mt[32 + 32 * i:64 + 32 * i]
            guid = uuid.UUID(bytes_le=e[:16])
            ioff, ilen = struct.unpack_from('<II', e, 16)
            items[guid] = self.read(moff + ioff, ilen)
        self.block_size, flags = struct.unpack_from('<II', items[FILE_PARAMS])
        self.has_parent = bool(flags & 2)
        self.size, = struct.unpack_from('<Q', items[VDISK_SIZE])
        self.sector, = struct.unpack_from('<I', items[LOGICAL_SECTOR])
        self.chunk_ratio = (1 << 23) * self.sector // self.block_size

        boff, blen = regions[BAT_GUID]
        self.bat = struct.unpack_from(f'<{blen // 8}Q', self.read(boff, blen))
        self.blocks = -(-self.size // self.block_size)

    def read(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def entry(self, idx):
        e = self.bat[idx]
        return e & 7, (e >> 20) * MB

    def payload(self, block):
        return self.entry(block + block // self.chunk_ratio)

    def bitmap(self, block):
        chunk = block // self.chunk_ratio
        state, off = self.entry(chunk * (self.chunk_ratio + 1) + self.chunk_ratio)
        if state != FULLY_PRESENT:
            return None
        sectors_per_block = self.block_size // self.sector
        start = (block % self.chunk_ratio) * sectors_per_block // 8
        return self.read(off + start, sectors_per_block // 8)


def runs(bitmap):
    """Yield (first_sector, count) runs of set bits (LSB-first within each byte)."""
    bits = int.from_bytes(bitmap, 'little')
    i, total = 0, len(bitmap) * 8
    while i < total:
        if bits >> i & 1:
            j = i
            while j < total and bits >> j & 1:
                j += 1
            yield i, j - i
            i = j
        else:
            i += 1


def main():
    out, layers = sys.argv[1], [Vhdx(p) for p in sys.argv[2:]]
    size = layers[-1].size
    assert all(l.size == size for l in layers), 'virtual sizes differ'
    assert not layers[0].has_parent, f'{layers[0].path} has a parent; pass the whole chain'
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.ftruncate(fd, size)
    for depth, l in enumerate(layers):
        copied = 0
        for b in range(l.blocks):
            state, off = l.payload(b)
            pos = b * l.block_size
            length = min(l.block_size, size - pos)
            if state == FULLY_PRESENT:
                os.pwrite(fd, l.read(off, length), pos)
                copied += length
            elif state in (ZERO, UNMAPPED) and depth:
                punch(fd, pos, length)
            elif state == PARTIALLY_PRESENT:
                bm = l.bitmap(b)
                assert bm is not None, f'{l.path}: block {b} partial but bitmap missing'
                for s, n in runs(bm):
                    a, ln = s * l.sector, n * l.sector
                    os.pwrite(fd, l.read(off + a, ln), pos + a)
                    copied += ln
        print(f'{os.path.basename(l.path)}: {copied / 2**30:.2f} GiB copied', flush=True)
    os.close(fd)


if __name__ == '__main__':
    main()
