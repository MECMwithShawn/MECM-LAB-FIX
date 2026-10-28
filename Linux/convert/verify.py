#!/usr/bin/env python3
"""Compare a flattened raw image with a vhdimount view over every block any layer
allocated, plus 200 random unallocated blocks. Usage: verify.py OUT.raw FUSE_IMG LAYER..."""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vhdxflat import Vhdx, NOT_PRESENT

out, ref, layers = sys.argv[1], sys.argv[2], [Vhdx(p) for p in sys.argv[3:]]
bs = 1 << 20
used = {m for l in layers for b in range(l.blocks) if l.payload(b)[0] != NOT_PRESENT
        for m in range(b * l.block_size // bs, (b + 1) * l.block_size // bs)}
rest = [b for b in random.sample(range(layers[0].size // bs), 2000) if b not in used][:200]
bad = 0
with open(out, 'rb') as a, open(ref, 'rb') as r:
    for b in sorted(used) + rest:
        a.seek(b * bs)
        r.seek(b * bs)
        if a.read(bs) != r.read(bs):
            bad += 1
            print('MISMATCH block', b)
print(f'checked {len(used)} allocated + {len(rest)} empty blocks of {bs // 2**20} MiB: {bad} mismatches')
sys.exit(1 if bad else 0)
