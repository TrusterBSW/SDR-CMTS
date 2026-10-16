# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""SYNC timestamp vs air position: fit timestamp = a * (symbol index of the timestamp byte) + b
and report the residual jitter (DOCSIS requires < 500 ns peak-to-peak)."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np
from cmts import rx_j83a as rx
from cmts.j83a import rrc_taps

path, fmt, fs = sys.argv[1], sys.argv[2], float(sys.argv[3])
x = rx.load_hackrf(path, fmt=fmt)
z = np.conj(rx.channelize(x, fs, 0))
z = np.convolve(z, rrc_taps(4, 16)).astype(np.complex64)
s, _ = rx.timing_recover(z)
s, _ = rx.freq_correct(s)
sym, eq, mer = rx.equalize(s, 256)
b = rx.symbols_to_bytes(rx.diff_decode(sym, 8), 8)
off, _ = rx.find_sync(b)
ts = rx.outer_decode(b[off:])
skip = 650000 // 204 + 20
pos, stamps, offs = [], [], []
for i in range(skip, ts.shape[0]):
    p = [int(v) for v in ts[i]]
    pid = ((int(p[1]) & 0x1F) << 8) | p[2]
    if pid != 0x1FFE or not p[1] & 0x40 or p[1] & 0x80:
        continue
    k = 5 + p[4]  # first frame starting in this packet
    while k + 32 <= 188 and p[k] != 0xFF:
        ln = (int(p[k + 2]) << 8) | p[k + 3]
        if p[k] == 0xC0 and p[k + 6 + 6 + 6 + 2 + 4] == 1:  # timing header + SYNC
            tb = k + 26  # timestamp offset in the packet
            n = i * 204 + tb  # RS-coded byte index (deinterleaved domain)
            sym_idx = n + 204 * (n % 12)  # 256-QAM: one byte per symbol after interleaving
            pos.append(sym_idx)
            stamps.append(int.from_bytes(bytes(p[tb:tb + 4]), "big"))
            offs.append(tb)
        k += 6 + ln
pos = np.array(pos, dtype=np.float64)
st = np.unwrap(np.array(stamps, dtype=np.float64), period=2 ** 32)
a, c = np.polyfit(pos, st, 1)
res = (st - (a * pos + c)) / 10.24  # in microseconds (1 count = 97.66 ns)
print(f"{len(pos)} SYNC; timestamp offsets in TS packet: {sorted(set(offs))}")
print(f"ts counts per symbol: {a:.6f} (nominal {1280 / 869:.6f}); residual jitter p-p {np.ptp(res) * 1000:.0f} ns, rms {np.std(res) * 1000:.0f} ns")
