# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""The downstream synthesis split over threads (cmts/radio.py TxChain): the same samples, to the
rounding of the float sums, whatever the number of threads (1, 2, 3, 6 for 6 channels), the
slices of the parallel sum covering the whole block, block after block (state of the encoders and
shapers carried over), and the accumulators reused when a block grows by a group of packets."""
import os
import sys
import threading

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import cdsp, radio  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


if not cdsp.DS_AVAILABLE:
    print("SKIP pas de compilateur C : dsdsp.c absent")
    sys.exit(77)
rng = np.random.default_rng(3)
NCH = 6


def params(th):
    steps = [2 * np.pi * (k - NCH / 2 + 0.5) * 8e6 / 55.616e6 for k in range(NCH)]
    return dict(qams=[256] * NCH, steps=steps, sps=8, invert=True, scale=0.7, synth_threads=th)


blocks = []
for npk in (64, 64, 72, 64, 56, 64):                 # a group of 8 packets more / fewer now and then
    blk = []
    for _ in range(NCH):
        p = rng.integers(0, 256, (npk, 188), dtype=np.uint8)
        p[:, 0] = 0x47
        blk.append(p)
    blocks.append(blk)
stop = threading.Event()
ref = radio.TxChain(params(1), stop)
want = [ref(b) for b in blocks]
for th in (2, 3, 6):
    c = radio.TxChain(params(th), stop)
    got = [c(b) for b in blocks]
    err = max(float(np.max(np.abs(g - w))) for g, w in zip(got, want))
    check(f"{th} fils = 1 fil, 6 blocs de tailles différentes", all(g.shape == w.shape for g, w in zip(got, want))
          and err < 1e-5, f"écart max {err:.1e}")
check("signal non nul et borné", 0.01 < float(np.mean(np.abs(want[0]))) and float(np.max(np.abs(want[0].real))) <= 1)
stop.set()
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
